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


# Before 2020 is not a real time: the clock was not set yet (the hardware clock lost its time,
# e.g. a long power-off; seen on the device as 1970-01-01 08:00:38). Say so instead of a 1970 date.
MIN_VALID_TS = 1577836800.0


def fmt_ts(t: float) -> str:
    if not t:
        return "-"
    text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    return text if t >= MIN_VALID_TS else f"{text} (clock not set yet: time unknown)"


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


def when(e: Dict) -> str:
    """The time of a record, from its number: its text was written with the clock of that moment, and
    a clock not set yet gave "1970-01-01 08:00:38" (fmt_ts says so instead)."""
    t = e.get("t")
    if not (isinstance(t, (int, float)) and t):
        return str(e.get("ts") or "-")
    return fmt_ts(t) + (" (clock set later)" if "t_unset" in e else "")


# Written at the start of a run, before its "start" record: they belong to that run.
_RUN_OPENERS = ("unclean_stop", "recorder_killed")


def _fix_unset_clock(records: List[Dict]) -> List[Dict]:
    """Records (in the order written) of a run whose clock was not set yet get their real time back.

    The Jetson has no clock kept over a power-off: every power-on starts at 1970 until GNSS time or
    NTP sets it, so the records of the first minute (the unclean stop of the run before, the start)
    said "1970" (seen 2026-10-21 at three power-ons, left out of "the last 24 h" and of `events`).
    The clock_step record of the same run gives the exact jump. The written time stays in "t_unset".
    A run that lost power before its clock was set leaves its records unknown."""
    pending: List[Dict] = []
    since_start: List[Dict] = []
    for e in records:
        kind = e.get("kind")
        if kind == "start":
            ours = {id(p) for p in since_start if p.get("kind") in _RUN_OPENERS}
            pending = [p for p in pending if id(p) in ours]
            since_start = []
        jump = e.get("jump_s")
        if kind == "clock_step" and pending and isinstance(jump, (int, float)):
            for p in pending:
                p["t_unset"], p["t"] = p["t"], p["t"] + float(jump)
            pending = []
        t = e.get("t")
        if kind != "clock_step" and isinstance(t, (int, float)) and t < MIN_VALID_TS:
            pending.append(e)
        since_start.append(e)
    return records


def _written(base: str) -> List[Dict]:
    """Every event record in the order written, times corrected (_fix_unset_clock)."""
    files = sorted(glob.glob(os.path.join(base, "events-*.jsonl.gz"))) + [os.path.join(base, "events.jsonl")]
    out: List[Dict] = []
    for p in files:
        out += list(_lines(p))
    return _fix_unset_clock(out)


def events(base: str, since: float, until: float, kinds: Optional[List[str]] = None,
           unknown_time: bool = False) -> List[Dict]:
    """unknown_time: also the records whose time stays unknown (written while the clock was not set,
    and not corrected), whatever the window: hiding them hid an unclean stop. They keep their place
    in the order written."""
    out, key = [], 0.0
    for e in _written(base):
        t = e.get("t", 0)
        known = isinstance(t, (int, float)) and t >= MIN_VALID_TS
        key = t if known else key                     # unknown: right after the record before it
        if (since <= t <= until if known else unknown_time) and (not kinds or e.get("kind") in kinds):
            out.append((key, e))
    return [e for _, e in sorted(out, key=lambda ke: ke[0])]


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
    """The newest unclean stop, by the order it was written, whatever its clock says. Right after a
    reboot the clock can be hours off (RTC in local time, no network yet), so a time window can
    miss the very record this command is for."""
    return next((e for e in reversed(_written(base)) if e.get("kind") == "unclean_stop"), None)


END_KINDS = ("stop", "unclean_stop", "recorder_killed")


def last_end(base: str) -> Optional[Dict]:
    """How the run before this one ended: the newest clean stop, unclean stop or killed recorder, by
    the order written (as last_unclean). An unclean stop is written when the next run starts, so the
    newest of the three is about the run that ended last."""
    return next((e for e in reversed(_written(base)) if e.get("kind") in END_KINDS), None)


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
