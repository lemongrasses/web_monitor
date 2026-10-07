"""Settings, and the thresholds derived from what this machine is."""

import copy
from typing import Dict, List, Optional

DEFAULTS: Dict = {
    "dir": "/var/log/aio-sysmon",
    "interval_s": 5.0,              # one full sample every this often (the daily log)
    "ring_interval_s": 1.0,         # one light record per second into the fixed-size ring
    "ring_mb": 4,                   # the ring: about 2 hours at 1 s, constant disk use
    "ring_sync_s": 5.0,             # flush the ring this often (a hard reset loses at most this much)
    "top_every_s": 60,              # top processes are included in a sample this often
    "burst_s": 60,                  # after a trigger: detailed sampling for this long ...
    "burst_interval_s": 1.0,        # ... at this interval (only while disk space is fine)
    "max_total_mb": None,           # hard cap for the whole directory; None: 0.3% of the disk, 60-250 MB
    "snapshots_share": 0.4,         # at most this share of the cap goes to snapshots
    "max_snapshots_per_day": 120,
    "events_max_mb": 10,
    "retention_days": 30,
    "min_free_mb": None,            # None: derived from the disk size (below this: no snapshots)
    "critical_free_mb": 512,        # below this: nothing but tiny events and last_known.json
    "kmsg": True,                   # copy kernel warnings/errors (needs root)
    "triggers": {},                 # override any derived limit, e.g. {"iowait_pct": 15}
    "cooldown_s": 120,
    "lock_memory": True,            # keep the recorder in RAM (needs root; ~20 MB)
    "escalate": True,               # when the machine stops responding: low real-time priority for a while
    "escalate_s": 60,
    "escalate_priority": 10,        # far below the kernel's own threads (50+)
    "escalate_on": None,            # None: stall, iowait, disk_wait_tasks, memory_low, swapping, swap_high, hot, slow_disk_write
}


def load(path: Optional[str] = None) -> Dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (OSError, ImportError):
            pass
    return cfg


def derive_limits(cap: Dict, overrides: Optional[Dict] = None) -> Dict:
    """Thresholds from the machine's own size: cores, RAM, swap, disk and thermal trip points."""
    cores = max(1, int(cap.get("cores", 1)))
    mem = float(cap.get("mem_total_mb", 0)) or 4096.0
    swap = float(cap.get("swap_total_mb", 0))
    disk = float(cap.get("disk_total_mb", 0)) or 32768.0
    hot = [t for t in cap.get("temp_trips_c", []) if t >= 80]
    limits = {
        "load1": round(1.5 * cores, 1),                 # run queue clearly longer than the cores
        "load_samples": 10,                             # for this many 1 s samples
        "cpu_busy_pct": 92.0, "cpu_busy_samples": 30,
        "iowait_pct": 25.0, "iowait_samples": 3,        # share of CPU time waiting for the disk
        "mem_avail_mb": round(max(512.0, 0.08 * mem)),
        "swap_used_pct": 25.0 if swap else None,
        "swap_out_pages_s": 2000,
        "temp_c": (min(hot) - 10.0) if hot else 85.0,   # 10 degrees below the first hot trip
        "blocked": 4, "blocked_samples": 3,             # tasks stuck waiting for the disk
        "sched_stall_ms": 800,                          # a 100 ms sleep that took this much longer
        "fsync_ms": 1000,
        "disk_free_mb": round(max(2048.0, 0.05 * disk)),
        "vin_drop_pct": 12.0,                           # input voltage below its recent median by this
    }
    limits.update({k: v for k, v in (overrides or {}).items() if k in limits})
    return limits


def default_max_total_mb(cap: Dict) -> float:
    return min(250.0, max(60.0, 0.003 * float(cap.get("disk_total_mb", 0))))


def default_min_free_mb(cap: Dict) -> float:
    return max(2048.0, 0.03 * float(cap.get("disk_total_mb", 0)))
