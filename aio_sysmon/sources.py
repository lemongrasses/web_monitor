"""Readers for the machine's vital signs (/proc, /sys). Every reader tolerates a missing file, so
the recorder runs on other kernels (it just records less). Paths can be redirected for tests."""

import os
import re
import time
from typing import Dict, List, Optional, Tuple

PROC = "/proc"
SYS = "/sys"
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _int(path: str) -> Optional[int]:
    s = _read(path)
    try:
        return int(s.strip()) if s else None
    except ValueError:
        return None


# ------------------------------------------------------------------ capacity (for thresholds)
def capacity() -> Dict:
    """What this machine is: the thresholds and budgets are derived from it, not hard-coded."""
    mem = meminfo()
    st = os.statvfs("/")
    temps = thermal_trips()
    return {
        "cores": os.cpu_count() or 1,
        "mem_total_mb": mem.get("mem_total_mb", 0),
        "swap_total_mb": mem.get("swap_total_mb", 0),
        "disk_total_mb": int(st.f_blocks * st.f_frsize / 1048576),
        "temp_trips_c": temps,
    }


def thermal_trips() -> List[float]:
    """Every trip temperature (deg C) of every thermal zone, ascending and unique."""
    found = set()
    base = f"{SYS}/class/thermal"
    try:
        zones = [z for z in os.listdir(base) if z.startswith("thermal_zone")]
    except OSError:
        return []
    for z in zones:
        for name in os.listdir(f"{base}/{z}"):
            if re.fullmatch(r"trip_point_\d+_temp", name):
                v = _int(f"{base}/{z}/{name}")
                if v:
                    found.add(round(v / 1000.0, 1))
    return sorted(found)


# --------------------------------------------------------------------------------- CPU
class CpuSampler:
    """CPU use between two calls: overall split, per core, context switches, interrupts."""

    def __init__(self):
        self._prev: Optional[Tuple[float, List[List[int]], int, int]] = None

    @staticmethod
    def _read():
        text = _read(f"{PROC}/stat")
        if not text:
            return None
        cpus, ctxt, intr, run, blk = [], 0, 0, 0, 0
        for line in text.splitlines():
            f = line.split()
            if not f:
                continue
            if f[0].startswith("cpu"):
                cpus.append([int(x) for x in f[1:9]] + [0] * (8 - len(f[1:9])))
            elif f[0] == "ctxt":
                ctxt = int(f[1])
            elif f[0] == "intr":
                intr = int(f[1])
            elif f[0] == "procs_running":
                run = int(f[1])
            elif f[0] == "procs_blocked":
                blk = int(f[1])
        return cpus, ctxt, intr, run, blk

    def sample(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        cur = self._read()
        if cur is None:
            return {}
        cpus, ctxt, intr, run, blk = cur
        out = {"run": run, "blk": blk}
        if self._prev is not None:
            t0, pc, c0, i0 = self._prev
            dt = max(now - t0, 1e-3)

            def split(a, b):
                d = [y - x for x, y in zip(a, b)]
                tot = max(sum(d[:8]), 1)
                user, nice, system, idle, iow, irq, sirq, steal = d[:8]
                return {"u": round(100.0 * (user + nice) / tot, 1), "s": round(100.0 * system / tot, 1),
                        "w": round(100.0 * iow / tot, 1), "i": round(100.0 * idle / tot, 1),
                        "irq": round(100.0 * irq / tot, 1), "si": round(100.0 * sirq / tot, 1),
                        "busy": round(100.0 * (tot - idle - iow) / tot, 1)}
            out["cpu"] = split(pc[0], cpus[0])
            out["pc"] = [split(a, b)["busy"] for a, b in zip(pc[1:], cpus[1:])]
            out["ctx"] = int((ctxt - c0) / dt)
            out["irq"] = int((intr - i0) / dt)
        self._prev = (now, cpus, ctxt, intr)
        return out


def loadavg() -> List[float]:
    s = _read(f"{PROC}/loadavg")
    try:
        return [float(x) for x in s.split()[:3]]
    except (AttributeError, ValueError):
        return []


def uptime_s() -> Optional[float]:
    s = _read(f"{PROC}/uptime")
    try:
        return float(s.split()[0])
    except (AttributeError, ValueError, IndexError):
        return None


def boot_time() -> Optional[int]:
    text = _read(f"{PROC}/stat") or ""
    m = re.search(r"^btime (\d+)", text, re.M)
    return int(m.group(1)) if m else None


def boot_id() -> str:
    return (_read(f"{PROC}/sys/kernel/random/boot_id") or "").strip()


# ------------------------------------------------------------------------------- memory
def meminfo() -> Dict:
    text = _read(f"{PROC}/meminfo")
    if not text:
        return {}
    kb = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts:
            kb[k] = int(parts[0])
    mb = lambda k: round(kb.get(k, 0) / 1024.0, 1)
    swap_total = mb("SwapTotal")
    return {"mem_total_mb": mb("MemTotal"), "avail_mb": mb("MemAvailable"), "free_mb": mb("MemFree"),
            "cache_mb": mb("Cached"), "dirty_mb": mb("Dirty"), "wb_mb": mb("Writeback"),
            "swap_total_mb": swap_total, "swap_used_mb": round(swap_total - mb("SwapFree"), 1)}


class SwapSampler:
    """Pages swapped in/out per second (swapping is what makes a machine feel stuck)."""

    def __init__(self):
        self._prev: Optional[Tuple[float, int, int, int]] = None

    def sample(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        text = _read(f"{PROC}/vmstat")
        if not text:
            return {}
        v = {}
        for line in text.splitlines():
            k, _, n = line.partition(" ")
            if k in ("pswpin", "pswpout", "pgmajfault"):
                v[k] = int(n)
        cur = (now, v.get("pswpin", 0), v.get("pswpout", 0), v.get("pgmajfault", 0))
        out = {}
        if self._prev:
            dt = max(now - self._prev[0], 1e-3)
            out = {"in": int((cur[1] - self._prev[1]) / dt), "out": int((cur[2] - self._prev[2]) / dt),
                   "maj": int((cur[3] - self._prev[3]) / dt)}
        self._prev = cur
        return out


# --------------------------------------------------------------------------------- disk
def root_disk_name() -> Optional[str]:
    """Whole-disk name behind / (nvme0n1 for /dev/nvme0n1p1)."""
    text = _read(f"{PROC}/mounts") or ""
    dev = None
    for line in text.splitlines():
        f = line.split()
        if len(f) > 1 and f[1] == "/" and f[0].startswith("/dev/"):
            dev = os.path.basename(f[0])
    if not dev:
        return None
    m = re.fullmatch(r"(nvme\d+n\d+|mmcblk\d+)p\d+", dev) or re.fullmatch(r"([a-z]+)\d+", dev)
    return m.group(1) if m else dev


class DiskSampler:
    """Busy %, throughput, average request time and queue depth of the root disk."""

    def __init__(self, name: Optional[str] = None):
        self.name = name or root_disk_name()
        self._prev = None

    def _row(self):
        for line in (_read(f"{PROC}/diskstats") or "").splitlines():
            f = line.split()
            if len(f) >= 14 and f[2] == self.name:
                return [int(x) for x in f[3:14]]
        return None

    def sample(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        row = self._row()
        if row is None:
            return {}
        out = {}
        if self._prev:
            t0, p = self._prev
            dt = max(now - t0, 1e-3)
            d = [b - a for a, b in zip(p, row)]
            rd, rsec, rms, wr, wsec, wms = d[0], d[2], d[3], d[4], d[6], d[7]
            ios = rd + wr
            out = {"u": round(min(100.0, d[9] / (dt * 10.0)), 1),           # busy %, ms spent doing IO
                   "r": round(rsec * 512 / 1048576 / dt, 2), "w": round(wsec * 512 / 1048576 / dt, 2),
                   "aw": round((rms + wms) / ios, 2) if ios else 0.0, "if": row[8]}
        self._prev = (now, row)
        return out


def disk_free_mb(path: str = "/") -> Tuple[float, float]:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1048576.0, st.f_blocks * st.f_frsize / 1048576.0


# ------------------------------------------------------------------------------ network
class NetSampler:
    """KB/s per interface, only for interfaces that are busy or have errors/drops."""

    def __init__(self):
        self._prev: Dict[str, Tuple[int, int, int]] = {}
        self._t = None

    def sample(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        cur = {}
        for line in (_read(f"{PROC}/net/dev") or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            f = rest.split()
            if len(f) >= 16:
                cur[name.strip()] = (int(f[0]), int(f[8]), int(f[2]) + int(f[3]) + int(f[10]) + int(f[11]))
        out = {}
        if self._t is not None:
            dt = max(now - self._t, 1e-3)
            for n, (rx, tx, bad) in cur.items():
                if n == "lo" or n not in self._prev:
                    continue
                p = self._prev[n]
                r, t, e = (rx - p[0]) / dt / 1024, (tx - p[1]) / dt / 1024, bad - p[2]
                if r > 1 or t > 1 or e > 0:
                    out[n] = [int(r), int(t), e]
        self._prev, self._t = cur, now
        return out


# -------------------------------------------------------------------- temperature / power
def temperatures() -> Dict[str, float]:
    out = {}
    base = f"{SYS}/class/thermal"
    try:
        zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
    except OSError:
        return out
    for z in zones:
        t, v = (_read(f"{base}/{z}/type") or z).strip(), _int(f"{base}/{z}/temp")
        if v is not None and v > -50000:
            out[t] = round(v / 1000.0, 1)
    return out


def power_rails() -> Dict[str, Dict]:
    """Voltage (mV) and current (mA) of each rail from an INA3221 power monitor, if present."""
    base = f"{SYS}/class/hwmon"
    try:
        hw = sorted(os.listdir(base))
    except OSError:
        return {}
    for h in hw:
        if (_read(f"{base}/{h}/name") or "").strip() != "ina3221":
            continue
        rails = {}
        for i in range(1, 4):
            label = (_read(f"{base}/{h}/in{i}_label") or "").strip()
            mv, ma = _int(f"{base}/{h}/in{i}_input"), _int(f"{base}/{h}/curr{i}_input")
            if label and mv is not None:
                rails[label] = {"mv": mv, "ma": ma}
        return rails
    return {}


def frequencies() -> Dict[str, int]:
    out = {}
    base = f"{SYS}/devices/system/cpu/cpufreq"
    try:
        for p in sorted(os.listdir(base)):
            v = _int(f"{base}/{p}/scaling_cur_freq")
            if v:
                out[f"c{p.replace('policy', '')}"] = v // 1000
    except OSError:
        pass
    g = _int(f"{SYS}/devices/platform/17000000.gpu/devfreq/17000000.gpu/cur_freq")
    if g:
        out["gpu"] = g // 1000000
    return out


def gpu_load_permille() -> Optional[int]:
    return _int(f"{SYS}/devices/platform/17000000.gpu/load")


# -------------------------------------------------------------------------------- processes
_STATE_NAMES = {"R": "running", "S": "sleeping", "D": "disk-wait", "Z": "zombie", "T": "stopped"}


class ProcessScanner:
    """Top CPU / memory consumers and tasks stuck in uninterruptible wait (D)."""

    def __init__(self):
        self._prev: Dict[int, Tuple[float, int]] = {}
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def scan(self, now: Optional[float] = None) -> List[Dict]:
        now = time.monotonic() if now is None else now
        procs, cur = [], {}
        try:
            names = [n for n in os.listdir(PROC) if n.isdigit()]
        except OSError:
            return procs
        for n in names:
            text = _read(f"{PROC}/{n}/stat")
            if not text:
                continue
            try:
                head, _, tail = text.rpartition(")")
                comm = head.partition("(")[2]
                f = tail.split()
                state, ticks, rss = f[0], int(f[11]) + int(f[12]), int(f[21]) * PAGE
            except (ValueError, IndexError):
                continue
            pid = int(n)
            cur[pid] = (now, ticks)
            cpu = 0.0
            if pid in self._prev:
                t0, k0 = self._prev[pid]
                cpu = 100.0 * (ticks - k0) / self._hz / max(now - t0, 1e-3)
            procs.append({"pid": pid, "name": comm, "state": state, "cpu": round(cpu, 1),
                          "rss_mb": round(rss / 1048576, 1)})
        self._prev = cur
        return procs

    @staticmethod
    def cmdline(pid: int, limit: int = 110) -> str:
        s = _read(f"{PROC}/{pid}/cmdline") or ""
        return s.replace("\0", " ").strip()[:limit]

    def top(self, procs: List[Dict], n: int = 8) -> Dict:
        by_cpu = sorted(procs, key=lambda p: p["cpu"], reverse=True)[:n]
        by_mem = sorted(procs, key=lambda p: p["rss_mb"], reverse=True)[:n]
        return {"cpu": [self._brief(p) for p in by_cpu if p["cpu"] > 0.5],
                "mem": [self._brief(p) for p in by_mem]}

    def _brief(self, p: Dict) -> Dict:
        return {"pid": p["pid"], "name": p["name"], "cpu": p["cpu"], "rss_mb": p["rss_mb"],
                "cmd": self.cmdline(p["pid"])}

    def dstate(self, procs: List[Dict]) -> List[Dict]:
        out = []
        for p in procs:
            if p["state"] != "D":
                continue
            item = self._brief(p)
            item["wchan"] = (_read(f"{PROC}/{p['pid']}/wchan") or "").strip()
            stack = _read(f"{PROC}/{p['pid']}/stack")          # root only
            if stack:
                item["stack"] = [l.strip() for l in stack.splitlines()[:8]]
            out.append(item)
        return out
