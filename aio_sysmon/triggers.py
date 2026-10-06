"""When is something worth recording in detail? Rules, debouncing and rate limiting.

A trigger needs the condition to hold for several samples (so one blip is ignored), and each
kind is silent for a cool-down afterwards, so a long problem produces a few snapshots and not
thousands.
"""

import statistics
from collections import deque
from typing import Dict, List


class TriggerEngine:
    def __init__(self, limits: Dict, cooldown_s: float = 120.0):
        self.limits = limits
        self.cooldown_s = cooldown_s
        self._count: Dict[str, int] = {}
        self._last_fire: Dict[str, float] = {}
        self._vin = deque(maxlen=600)          # input voltage over the last ~10 minutes
        self.active: Dict[str, str] = {}       # kind -> message while the condition holds

    # ------------------------------------------------------------------------- helpers
    def _hold(self, kind: str, cond: bool, need: int) -> bool:
        self._count[kind] = (self._count.get(kind, 0) + 1) if cond else 0
        return self._count[kind] >= need

    def _fire(self, out: List[Dict], kind: str, now: float, msg: str, value, limit, cooldown=None) -> None:
        self.active[kind] = msg
        wait = self.cooldown_s if cooldown is None else cooldown
        if now - self._last_fire.get(kind, -1e18) >= wait:
            self._last_fire[kind] = now
            out.append({"kind": kind, "msg": msg, "value": value, "limit": limit})

    def evaluate(self, s: Dict) -> List[Dict]:
        """`s` is a light 1 s record. Returns the triggers that fire now."""
        L, now, out = self.limits, s["t"], []
        self.active = {k: v for k, v in self.active.items() if k in self._count and self._count[k] > 0}

        def held(kind, cond, need, msg, value, limit, cooldown=None):
            if self._hold(kind, bool(cond), need):
                self._fire(out, kind, now, msg, value, limit, cooldown)
            elif not cond:
                self.active.pop(kind, None)

        if "l" in s:
            held("load", s["l"] > L["load1"], int(L["load_samples"]),
                 f"load average {s['l']} is above {L['load1']} for {L['load_samples']} s", s["l"], L["load1"])
        if "b" in s:
            held("cpu_saturated", s["b"] > L["cpu_busy_pct"], int(L["cpu_busy_samples"]),
                 f"CPU {s['b']}% busy for {L['cpu_busy_samples']} s", s["b"], L["cpu_busy_pct"])
        if "w" in s:
            held("iowait", s["w"] > L["iowait_pct"], int(L["iowait_samples"]),
                 f"{s['w']}% of CPU time waiting for the disk", s["w"], L["iowait_pct"])
        if "a" in s:
            held("memory_low", s["a"] < L["mem_avail_mb"], 2,
                 f"only {s['a']} MB of memory available", s["a"], L["mem_avail_mb"])
        if "s" in s and L["swap_used_pct"] and s.get("swap_total"):
            pct = 100.0 * s["s"] / s["swap_total"]
            held("swap_high", pct > L["swap_used_pct"], 3,
                 f"swap {pct:.0f}% used", round(pct, 1), L["swap_used_pct"])
        if "o" in s:
            held("swapping", s["o"] > L["swap_out_pages_s"], 2,
                 f"swapping out {s['o']} pages/s", s["o"], L["swap_out_pages_s"])
        if "T" in s:
            held("hot", s["T"] > L["temp_c"], 3, f"temperature {s['T']} C", s["T"], L["temp_c"])
        if "k" in s:
            held("disk_wait_tasks", s["k"] >= L["blocked"], int(L["blocked_samples"]),
                 f"{s['k']} tasks stuck waiting for the disk", s["k"], L["blocked"])
        if "sc" in s:
            held("stall", s["sc"] > L["sched_stall_ms"], 1,
                 f"the system stalled: a 100 ms sleep took {s['sc']} ms longer", s["sc"], L["sched_stall_ms"])
        if "fs" in s and s["fs"] is not None:
            held("slow_disk_write", s["fs"] > L["fsync_ms"], 1,
                 f"writing to the disk took {s['fs']} ms", s["fs"], L["fsync_ms"])
        if "f" in s:
            held("disk_space", s["f"] < L["disk_free_mb"], 5,
                 f"only {s['f']:.0f} MB of disk space left", s["f"], L["disk_free_mb"], cooldown=3600.0)
        if "v" in s and s["v"]:
            self._vin.append(s["v"])
            if len(self._vin) >= 60:
                med = statistics.median(self._vin)
                low = med * (1 - L["vin_drop_pct"] / 100.0)
                held("power_dip", s["v"] < low, 1,
                     f"input voltage {s['v']} mV, {100 * (1 - s['v'] / med):.0f}% below its recent {med:.0f} mV",
                     s["v"], round(low))
        return out
