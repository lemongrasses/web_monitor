"""The recorder loop: one light record per second, a fuller sample every few seconds, detail
only when something looks wrong, and a verdict on how the previous run ended."""

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Dict, List, Optional

from . import __version__, config as cfgmod, sources
from .kmsg import KmsgReader
from .probes import LatencyProbe
from .ring import RingFile, SLOT, read_ring
from .store import CRITICAL, OK, Store
from .triggers import TriggerEngine


def _round(x, n=1):
    return None if x is None else round(x, n)


class Daemon:
    def __init__(self, cfg: Dict, store: Optional[Store] = None, wall=time.time, mono=time.monotonic,
                 probe: Optional[LatencyProbe] = None, kmsg: Optional[KmsgReader] = None):
        self.cfg = cfg
        self.cap = sources.capacity()
        if cfg.get("min_free_mb") is None:
            cfg["min_free_mb"] = cfgmod.default_min_free_mb(self.cap)
        if cfg.get("max_total_mb") is None:
            cfg["max_total_mb"] = cfgmod.default_max_total_mb(self.cap)
        self.limits = cfgmod.derive_limits(self.cap, cfg.get("triggers"))
        self.wall, self.mono = wall, mono
        self.store = store or Store(cfg, clock=wall)
        self.ring = RingFile(self.store.path("ring.bin"), int(cfg["ring_mb"] * 1048576))
        self.engine = TriggerEngine(self.limits, float(cfg["cooldown_s"]))
        self.probe = probe or LatencyProbe()
        self.kmsg = kmsg if kmsg is not None else (KmsgReader(self.store.write_event) if cfg.get("kmsg") else None)
        # samplers: one set for the 1 s light record, one for the longer full sample
        self.cpu1, self.disk1 = sources.CpuSampler(), sources.DiskSampler()
        self.swap1 = sources.SwapSampler()
        self.cpuN, self.diskN, self.netN, self.swapN = (sources.CpuSampler(), sources.DiskSampler(),
                                                        sources.NetSampler(), sources.SwapSampler())
        self.scanner = sources.ProcessScanner()
        self.recent: deque = deque(maxlen=180)       # the last 3 minutes of light records (for snapshots)
        self._pending: List[Dict] = []
        self._stop = threading.Event()
        self.burst_until = 0.0
        self._next_full = 0.0
        self._next_sync = 0.0
        self._next_top = 0.0
        self._next_budget = 0.0
        self._next_known = 0.0
        self._last_fsync_ms: Optional[float] = None
        self._last_tick: Optional[float] = None
        self._last_wall_minus_mono: Optional[float] = None
        self.boot_id = sources.boot_id()

    # ------------------------------------------------------------------- previous run
    def boot_analysis(self) -> Optional[Dict]:
        """Was the previous run ended cleanly? If not, say what the last readings looked like."""
        run_path = self.store.path("run.json")
        verdict = None
        try:
            with open(run_path) as f:
                run = json.load(f)
        except (OSError, ValueError):
            run = None
        if run is not None and not run.get("clean"):
            verdict = analyze_unclean(run, self._read_json(self.store.path("last_known.json")),
                                      self.ring.read_all(), self.boot_id, self.wall(),
                                      self.kernel_events_since(run.get("last_t", 0) - 120))
            kind = "unclean_stop" if not verdict["same_boot"] else "recorder_killed"
            self.store.write_event(kind, verdict)
            self.store.write_snapshot("postmortem", {"verdict": verdict, "ring_tail": self.ring.read_all()[-600:]})
        with open(run_path + ".tmp", "w") as f:
            json.dump({"pid": os.getpid(), "start": self.wall(), "boot_id": self.boot_id, "clean": False,
                       "version": __version__}, f)
        os.replace(run_path + ".tmp", run_path)
        return verdict

    @staticmethod
    def _read_json(path: str):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def kernel_events_since(self, t: float) -> List[Dict]:
        out = []
        try:
            with open(self.store.path("events.jsonl")) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("kind") == "kernel" and e.get("t", 0) >= t:
                        out.append({"ts": e.get("ts"), "msg": e.get("msg")})
        except OSError:
            pass
        return out[-15:]

    def mark_clean_stop(self) -> None:
        run_path = self.store.path("run.json")
        run = self._read_json(run_path) or {}
        run.update({"clean": True, "stop": self.wall()})
        with open(run_path + ".tmp", "w") as f:
            json.dump(run, f)
        os.replace(run_path + ".tmp", run_path)

    # --------------------------------------------------------------------------- samples
    def light_sample(self, now: float, m: float) -> Dict:
        cpu = self.cpu1.sample(m)
        mem = sources.meminfo()
        ld = sources.loadavg()
        disk = self.disk1.sample(m)
        swp = self.swap1.sample(m)
        temps = sources.temperatures()
        rails = sources.power_rails()
        free, _ = sources.disk_free_mb()
        sc, _ = self.probe.take()
        rec = {"t": round(now, 2), "u": int(sources.uptime_s() or 0)}
        if cpu.get("cpu"):
            rec.update({"b": cpu["cpu"]["busy"], "w": cpu["cpu"]["w"]})
        rec["k"], rec["r"] = cpu.get("blk", 0), cpu.get("run", 0)
        if ld:
            rec["l"] = ld[0]
        if mem:
            rec["a"], rec["s"] = int(mem["avail_mb"]), int(mem["swap_used_mb"])
        if swp:
            rec["o"] = swp["out"]
        if temps:
            rec["T"] = max(temps.values())
        rec["sc"], rec["f"] = sc, int(free)
        if self._last_fsync_ms is not None:
            rec["fs"] = _round(self._last_fsync_ms)
        if disk:
            rec["d"], rec["aw"] = disk["u"], disk["aw"]
        vin = rails.get("VDD_IN")
        if vin:
            rec["v"] = vin["mv"]
        return rec

    def full_sample(self, now: float, m: float, with_top: bool, mode: str) -> Dict:
        cpu = self.cpuN.sample(m)
        mem = sources.meminfo()
        rec = {"t": round(now, 2), "u": int(sources.uptime_s() or 0), "mode": mode, "boot": self.boot_id[:8]}
        rec.update({k: v for k, v in cpu.items()})
        rec["ld"] = sources.loadavg()
        rec["mem"] = {k: mem[k] for k in ("avail_mb", "free_mb", "cache_mb", "dirty_mb", "wb_mb", "swap_used_mb") if k in mem}
        rec["swp"] = self.swapN.sample(m)
        rec["dsk"] = self.diskN.sample(m)
        rec["dsk"]["free_mb"] = int(sources.disk_free_mb()[0])
        rec["net"] = self.netN.sample(m)
        rec["tmp"] = sources.temperatures()
        rec["pw"] = sources.power_rails()
        rec["fq"] = sources.frequencies()
        g = sources.gpu_load_permille()
        if g is not None:
            rec["gpu"] = g
        if with_top:
            procs = self.scanner.scan(m)
            rec["top"] = self.scanner.top(procs, 5)
            rec["top"]["cpu"] = [{k: p[k] for k in ("pid", "name", "cpu", "rss_mb")} for p in rec["top"]["cpu"]]
            rec["top"]["mem"] = [{k: p[k] for k in ("pid", "name", "cpu", "rss_mb")} for p in rec["top"]["mem"]]
        return rec

    # ----------------------------------------------------------------------- reactions
    def on_trigger(self, t: Dict, light: Dict, m: float) -> None:
        self.store.write_event("anomaly", {"trigger": t["kind"], "msg": t["msg"], "value": t["value"],
                                           "limit": t["limit"], "light": light})
        level = self.store.disk_level()
        if level == OK:
            self.burst_until = max(self.burst_until, m + float(self.cfg["burst_s"]))
            path = self.store.write_snapshot(t["kind"], self.snapshot(t, light, m))
            if path:
                self.store.write_event("snapshot", {"trigger": t["kind"], "file": os.path.basename(path)})

    def snapshot(self, trigger: Dict, light: Dict, m: float) -> Dict:
        procs = self.scanner.scan(m)
        time.sleep(1.0)                                        # a second apart: CPU % per process
        procs = self.scanner.scan(self.mono())
        return {"version": __version__, "trigger": trigger, "light": light,
                "recent": list(self.recent)[-120:], "top": self.scanner.top(procs, 12),
                "disk_wait_tasks": self.scanner.dstate(procs),
                "meminfo": sources.meminfo(), "loadavg": sources.loadavg(),
                "net": self.netN.sample(self.mono()), "temps": sources.temperatures(),
                "rails": sources.power_rails(), "freq": sources.frequencies(),
                "gpu_permille": sources.gpu_load_permille(),
                "kernel": self.kmsg.tail(40) if self.kmsg else [], "limits": self.limits}

    # ------------------------------------------------------------------------- the loop
    def tick(self) -> None:
        now, m = self.wall(), self.mono()
        # the recorder itself running late is evidence of a system-wide stall
        if self._last_tick is not None and m - self._last_tick > 3.0 * float(self.cfg["ring_interval_s"]) + 1.0:
            self.store.write_event("tick_gap", {"gap_s": round(m - self._last_tick, 1)}, now)
        self._last_tick = m
        off = now - m
        if self._last_wall_minus_mono is not None and abs(off - self._last_wall_minus_mono) > 5.0:
            self.store.write_event("clock_step", {"jump_s": round(off - self._last_wall_minus_mono, 1)}, now)
        self._last_wall_minus_mono = off

        light = self.light_sample(now, m)
        self.recent.append(light)
        self._pending.append(light)
        view = dict(light, swap_total=self.cap.get("swap_total_mb"))
        for t in self.engine.evaluate(view):
            self.on_trigger(t, light, m)

        level = self.store.disk_level()
        bursting = m < self.burst_until and level == OK
        # the ring: flushed in small batches (every second while bursting)
        if m >= self._next_sync or bursting:
            self.ring.append_many(self._pending)
            self._pending.clear()
            self.ring.sync()
            self._next_sync = m + float(self.cfg["ring_sync_s"])

        interval = float(self.cfg["burst_interval_s"] if bursting else self.cfg["interval_s"])
        if level == CRITICAL:
            interval = max(interval, 60.0)
        if m >= self._next_full:
            with_top = m >= self._next_top
            full = self.full_sample(now, m, with_top, "burst" if bursting else "normal")
            self._last_fsync_ms = self.store.write_sample(full)
            if m >= self._next_known:                    # the ring already has the newest records: this is a backstop
                self.store.write_last_known(dict(light, load=full.get("ld"), up=light.get("u")))
                self._next_known = m + 30.0
            self._next_full = m + interval
            if with_top:
                self._next_top = m + float(self.cfg["top_every_s"])
        if m >= self._next_budget:
            self.store.enforce_budget()
            self._next_budget = m + 600.0

    def run(self) -> None:
        verdict = self.boot_analysis()
        self.store.write_event("start", {"version": __version__, "boot_id": self.boot_id[:8], "capacity": self.cap,
                                         "limits": self.limits, "kmsg": bool(self.kmsg and self.kmsg.start()),
                                         "previous_run": (verdict or {}).get("summary", "clean")})
        self.probe.start()
        # prime the samplers so the first record has rates
        self.light_sample(self.wall(), self.mono())
        self.full_sample(self.wall(), self.mono(), False, "normal")
        self._next_full = self.mono() + float(self.cfg["interval_s"])     # first real sample a full interval later
        period = float(self.cfg["ring_interval_s"])
        nxt = self.mono()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:                              # the recorder must never die of a bad reading
                self.store.write_event("recorder_error", {"error": repr(e)[:200]})
            nxt += period
            if nxt <= self.mono():                       # we fell behind (a stall): skip the missed
                nxt = self.mono() + period               # seconds instead of racing to catch up with
            self._stop.wait(max(0.0, nxt - self.mono()))  # rates computed over almost no time
        self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        self.probe.stop()
        if self.kmsg:
            self.kmsg.stop()
        try:
            self.ring.append_many(self._pending)
            self.ring.sync()
        except OSError:
            pass
        self.store.write_event("stop", {"clean": True})
        self.mark_clean_stop()
        self.ring.close()
        self.store.close()


def analyze_unclean(run: Dict, last_known: Optional[Dict], ring: List[Dict], boot_id_now: str,
                    now: float, kernel_tail: List[Dict]) -> Dict:
    """How did the previous run end, and what did the machine look like in its last minute?"""
    last_t = max([r.get("t", 0) for r in ring[-5:]] + [(last_known or {}).get("t", 0)] or [0])
    same_boot = run.get("boot_id") == boot_id_now
    tail = [r for r in ring if r.get("t", 0) >= last_t - 60]
    L = {}
    if tail:
        mx = lambda k: max((r[k] for r in tail if k in r), default=None)
        mn = lambda k: min((r[k] for r in tail if k in r), default=None)
        L = {"temp_max_c": mx("T"), "load1_max": mx("l"), "iowait_max_pct": mx("w"), "mem_avail_min_mb": mn("a"),
             "swap_used_max_mb": mx("s"), "stall_max_ms": mx("sc"), "fsync_max_ms": mx("fs"),
             "disk_wait_tasks_max": mx("k"), "vin_min_mv": mn("v"), "vin_max_mv": mx("v"),
             "free_disk_min_mb": mn("f"), "records": len(tail)}
    hint = "readings were normal right up to the last record: a sudden power loss, a hard reset, or a hang that gave no warning"
    if L:
        lim = (run.get("limits") or {})
        if L.get("vin_min_mv") and L.get("vin_max_mv") and L["vin_min_mv"] < 0.88 * L["vin_max_mv"]:
            hint = "the input voltage fell in the last minute (power supply or cable)"
        elif (L.get("temp_max_c") or 0) >= 85:
            hint = "the temperature was very high (overheating)"
        elif (L.get("mem_avail_min_mb") is not None and L["mem_avail_min_mb"] < 400) or (L.get("swap_used_max_mb") or 0) > 8000:
            hint = "memory was running out (swapping or exhausted)"
        elif (L.get("iowait_max_pct") or 0) > 40 or (L.get("fsync_max_ms") or 0) > 2000 or (L.get("disk_wait_tasks_max") or 0) >= 4:
            hint = "the disk was stalling (long waits, tasks stuck on disk)"
        elif (L.get("stall_max_ms") or 0) > 800:
            hint = "the system was stalling right before the end"
    summary = ("the recorder was killed but the machine kept running" if same_boot else
               f"the machine stopped without a clean shutdown; {hint}")
    return {"same_boot": same_boot, "last_record_t": last_t,
            "last_record": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_t)) if last_t else None,
            "previous_boot": (run.get("boot_id") or "")[:8], "last_minute": L, "hint": hint,
            "kernel_before_end": kernel_tail, "summary": summary}


def main_loop(cfg_path: Optional[str] = None) -> int:
    cfg = cfgmod.load(cfg_path)
    d = Daemon(cfg)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: d.stop())
    d.run()
    return 0
