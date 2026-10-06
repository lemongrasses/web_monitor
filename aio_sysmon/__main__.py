"""aio-sysmon: record and read the machine's black box.

  aio-sysmon run            the recorder (started by systemd)
  aio-sysmon status         is it running, how much space it uses, latest reading
  aio-sysmon last-crash     how the previous run ended and the last minute before it
  aio-sysmon report         hour-by-hour summary (default: last 6 hours) and the notable events
  aio-sysmon ring           the newest second-by-second records
  aio-sysmon events         anomalies, kernel warnings, starts and stops
  aio-sysmon snapshots      the detailed snapshots; `show NAME` prints one
"""

import argparse
import glob
import json
import os
import sys
import time

from . import __version__, config as cfgmod, report as rp, sources
from .ring import read_ring

DEFAULT_CONFIG = "/opt/aio-dashboard/config/sysmon.yaml"


def _base(args) -> str:
    return args.dir or cfgmod.load(args.config if args.config and os.path.exists(args.config) else None)["dir"]


def cmd_status(args) -> int:
    base = _base(args)
    cfg = cfgmod.load(args.config if os.path.exists(args.config or "") else None)
    used = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(base) for f in fs) if os.path.isdir(base) else 0
    lk = None
    try:
        with open(os.path.join(base, "last_known.json")) as f:
            lk = json.load(f)
    except (OSError, ValueError):
        pass
    st = os.statvfs("/")
    print(f"aio-sysmon {__version__}   directory {base}")
    cap_mb = cfg["max_total_mb"] if cfg["max_total_mb"] is not None else cfgmod.default_max_total_mb(sources.capacity())
    print(f"space used: {used / 1048576:.1f} MB of the {cap_mb:.0f} MB cap;  disk free: {st.f_bavail * st.f_frsize / 1048576 / 1024:.1f} GB")
    if lk:
        print(f"latest record: {rp.fmt_ts(lk['t'])} ({time.time() - lk['t']:.0f} s ago)  cpu {lk.get('b')}%  load {lk.get('l')}  "
              f"mem avail {lk.get('a', '-')} MB  temp {lk.get('T', '-')} C  vin {lk.get('v', '-')} mV  "
              f"disk write {lk.get('fs', '-')} ms")
        if time.time() - lk["t"] > 30:
            print("WARNING: the recorder is not writing (last record is older than 30 s)")
    else:
        print("no records yet")
    ev = rp.events(base, time.time() - 86400, time.time() + 1, ["anomaly", "unclean_stop"])
    print(f"in the last 24 h: {sum(1 for e in ev if e['kind'] == 'anomaly')} anomalies, "
          f"{sum(1 for e in ev if e['kind'] == 'unclean_stop')} unclean stops")
    return 0


def cmd_report(args) -> int:
    base = _base(args)
    until = time.time()
    since = time.mktime(time.strptime(args.since, "%Y-%m-%d %H:%M")) if args.since else until - args.hours * 3600
    ss = rp.samples(base, since, until)
    print(f"report {rp.fmt_ts(since)} -> {rp.fmt_ts(until)}   ({len(ss)} samples)")
    rows = rp.hourly(ss)
    if rows:
        print(rp.table(rows, [("hour", "hour", 11), ("n", "n", 5), ("cpu_avg", "cpu avg%", 8), ("cpu_max", "cpu max%", 8),
                              ("load_max", "load max", 8), ("iowait_max", "iowait%", 7), ("mem_min", "mem min MB", 10),
                              ("swap_max", "swap MB", 7), ("temp_max", "temp max", 8), ("vin_min", "vin min mV", 10),
                              ("disk_busy_max", "disk% max", 9), ("free_min_mb", "free MB", 8)]))
    else:
        print("no samples in this window")
    notable = rp.events(base, since, until, ["anomaly", "unclean_stop", "recorder_killed", "kernel", "tick_gap", "clock_step", "start"])
    print(f"\nnotable events ({len(notable)}):")
    for e in notable[-60:]:
        extra = e.get("msg") or e.get("summary") or e.get("gap_s") or e.get("jump_s") or ""
        print(f"  {e['ts']}  {e['kind']:15s} {str(e.get('trigger', '')):16s} {extra}"[:170])
    return 0


def cmd_last_crash(args) -> int:
    base = _base(args)
    e = rp.last_unclean(base)
    if not e:
        print("no unclean stop has been recorded (every previous run ended cleanly)")
        return 0
    print(f"unclean stop recorded at {e['ts']}\n")
    print(f"verdict:  {e.get('summary')}")
    print(f"last record before the stop: {e.get('last_record')}   (previous boot {e.get('previous_boot')})")
    lm = e.get("last_minute") or {}
    if lm:
        print("last minute: " + ", ".join(f"{k}={v}" for k, v in lm.items() if v is not None))
    if e.get("kernel_before_end"):
        print("kernel messages shortly before the end:")
        for k in e["kernel_before_end"]:
            print(f"  {k.get('ts')}  {k.get('msg')}")
    ring = [r for r in read_ring(os.path.join(base, "ring.bin")) if r.get("t", 0) <= (e.get("last_record_t") or 0) + 1]
    print("\nthe last records second by second (ring):")
    print(rp.ring_table(ring[-args.rows:]))
    return 0


def cmd_ring(args) -> int:
    recs = read_ring(os.path.join(_base(args), "ring.bin"))
    print(f"ring: {len(recs)} records, {rp.fmt_ts(recs[0]['t']) if recs else '-'} -> {rp.fmt_ts(recs[-1]['t']) if recs else '-'}")
    print(rp.ring_table(recs[-args.rows:]))
    return 0


def cmd_events(args) -> int:
    until = time.time() + 1
    for e in rp.events(_base(args), until - args.hours * 3600, until, [args.kind] if args.kind else None)[-args.rows:]:
        text = e.get("msg") or e.get("summary") or ""
        if not text:
            text = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "ts", "kind", "light", "capacity", "limits"))
        print(f"{e['ts']}  {e['kind']:15s} {str(e.get('trigger', '')):16s} {text}"[:200])
    return 0


def cmd_snapshots(args) -> int:
    base = _base(args)
    files = sorted(glob.glob(os.path.join(base, "snapshots", "snap-*.json.gz")))
    if args.name:
        match = [f for f in files if args.name in f]
        if not match:
            print("no such snapshot")
            return 1
        print(rp.describe_snapshot(match[-1]))
        return 0
    for f in files[-args.rows:]:
        print(f"{os.path.getsize(f) / 1024:7.1f} KB  {os.path.basename(f)}")
    print(f"{len(files)} snapshots")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="aio-sysmon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dir", help="log directory (default: from the config)")
    # --config / --dir are accepted before or after the command (the service uses "run --config ...")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--dir", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", parents=[common])
    sub.add_parser("status", parents=[common])
    r = sub.add_parser("report", parents=[common])
    r.add_argument("--hours", type=float, default=6)
    r.add_argument("--since", help='"YYYY-MM-DD HH:MM"')
    c = sub.add_parser("last-crash", parents=[common])
    c.add_argument("--rows", type=int, default=90)
    g = sub.add_parser("ring", parents=[common])
    g.add_argument("--rows", type=int, default=60)
    e = sub.add_parser("events", parents=[common])
    e.add_argument("--hours", type=float, default=24)
    e.add_argument("--kind")
    e.add_argument("--rows", type=int, default=100)
    s = sub.add_parser("snapshots", parents=[common])
    s.add_argument("name", nargs="?")
    s.add_argument("--rows", type=int, default=40)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "run":
        from .daemon import main_loop
        return main_loop(args.config if os.path.exists(args.config) else None)
    handlers = {"status": cmd_status, "report": cmd_report, "last-crash": cmd_last_crash, "ring": cmd_ring,
                "events": cmd_events, "snapshots": cmd_snapshots}
    if args.cmd not in handlers:
        ap.print_help()
        return 1
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
