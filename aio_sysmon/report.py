"""Reading the records back: what happened, hour by hour, and how the last run ended."""

import glob
import gzip
import json
import os
import time
from typing import Dict, Iterator, List, Optional

from .ring import read_ring


def _lines(path: str) -> Iterator[Dict]:
    op = gzip.open if path.endswith(".gz") else open
    try:
        with op(path, "rt", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (OSError, EOFError):
        return


def fmt_ts(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def samples(base: str, since: float, until: float) -> List[Dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(base, "samples", "samples-*"))):
        day = os.path.basename(p).split("-")[1][:8]
        try:
            lo = time.mktime(time.strptime(day, "%Y%m%d"))
        except ValueError:
            continue
        if lo + 86400 < since or lo > until:
            continue
        out += [s for s in _lines(p) if since <= s.get("t", 0) <= until]
    return sorted(out, key=lambda s: s.get("t", 0))


def events(base: str, since: float, until: float, kinds: Optional[List[str]] = None) -> List[Dict]:
    files = sorted(glob.glob(os.path.join(base, "events-*.jsonl.gz"))) + [os.path.join(base, "events.jsonl")]
    out = []
    for p in files:
        out += [e for e in _lines(p) if since <= e.get("t", 0) <= until and (not kinds or e.get("kind") in kinds)]
    return sorted(out, key=lambda e: e.get("t", 0))


def hourly(samples_: List[Dict]) -> List[Dict]:
    buckets: Dict[str, List[Dict]] = {}
    for s in samples_:
        buckets.setdefault(time.strftime("%m-%d %H:00", time.localtime(s["t"])), []).append(s)
    rows = []
    for hour, ss in buckets.items():
        g = lambda f: [v for v in (f(s) for s in ss) if v is not None]
        busy = g(lambda s: (s.get("cpu") or {}).get("busy"))
        iow = g(lambda s: (s.get("cpu") or {}).get("w"))
        rows.append({"hour": hour, "n": len(ss),
                     "cpu_avg": round(sum(busy) / len(busy), 1) if busy else None,
                     "cpu_max": max(busy) if busy else None,
                     "load_max": max(g(lambda s: (s.get("ld") or [None])[0]), default=None),
                     "iowait_max": max(iow) if iow else None,
                     "mem_min": min(g(lambda s: (s.get("mem") or {}).get("avail_mb")), default=None),
                     "swap_max": max(g(lambda s: (s.get("mem") or {}).get("swap_used_mb")), default=None),
                     "temp_max": max(g(lambda s: max((s.get("tmp") or {"x": None}).values(), default=None)), default=None),
                     "vin_min": min(g(lambda s: ((s.get("pw") or {}).get("VDD_IN") or {}).get("mv")), default=None),
                     "disk_busy_max": max(g(lambda s: (s.get("dsk") or {}).get("u")), default=None),
                     "free_min_mb": min(g(lambda s: (s.get("dsk") or {}).get("free_mb")), default=None)})
    return rows


def table(rows: List[Dict], cols: List[tuple]) -> str:
    """cols: (key, header, width)"""
    head = "  ".join(h.rjust(w) for _, h, w in cols)
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append("  ".join(("-" if r.get(k) is None else str(r.get(k))).rjust(w) for k, _, w in cols))
    return "\n".join(lines)


RING_COLS = [("ts", "time", 19), ("u", "uptime", 7), ("b", "cpu%", 5), ("w", "iowait", 6), ("l", "load", 5), ("a", "memavail", 8),
             ("s", "swap", 5), ("T", "temp", 5), ("k", "blk", 3), ("sc", "stall_ms", 8), ("fs", "fsync", 6),
             ("d", "disk%", 5), ("aw", "await", 5), ("v", "vin_mV", 7), ("f", "free_MB", 8)]


def ring_table(recs: List[Dict]) -> str:
    rows = [dict(r, ts=fmt_ts(r.get("t", 0))) for r in recs]
    return table(rows, RING_COLS)


def last_unclean(base: str) -> Optional[Dict]:
    evs = events(base, 0, time.time() + 1, ["unclean_stop"])
    return evs[-1] if evs else None


def describe_snapshot(path: str) -> str:
    with gzip.open(path, "rt") as f:
        s = json.load(f)
    trig = s.get("trigger", {})
    out = [f"snapshot {os.path.basename(path)}", f"trigger: {trig.get('kind')} - {trig.get('msg')}"]
    light = s.get("light") or {}
    out.append("at the time: " + ", ".join(f"{k}={v}" for k, v in light.items() if k not in ("t",)))
    top = s.get("top") or {}
    out.append("top CPU: " + "; ".join(f"{p['name']}[{p['pid']}] {p['cpu']}%" for p in top.get("cpu", [])[:8]))
    out.append("top memory: " + "; ".join(f"{p['name']}[{p['pid']}] {p['rss_mb']} MB" for p in top.get("mem", [])[:6]))
    dw = s.get("disk_wait_tasks") or []
    out.append("tasks stuck on disk: " + ("none" if not dw else "; ".join(f"{p['name']}[{p['pid']}] wchan={p.get('wchan')}" for p in dw)))
    out.append("temperatures: " + ", ".join(f"{k}={v}" for k, v in (s.get("temps") or {}).items()))
    rails = s.get("rails") or {}
    out.append("power rails: " + ", ".join(f"{k}={v.get('mv')}mV/{v.get('ma')}mA" for k, v in rails.items()))
    k = s.get("kernel") or []
    out.append(f"kernel messages (last {min(len(k), 15)}):")
    out += ["  " + m for m in k[-15:]]
    return "\n".join(out)
