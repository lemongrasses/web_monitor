#!/usr/bin/env python3
"""Long test of GNSS time from the IMU stamps (collectors/gnss_time.py) against an NTP clock.

Run while chrony is synced to NTP (the reference) and has the GNSS source in shadow mode:

    /etc/chrony/conf.d/aio-gnss.conf:  refclock SHM 2:perm=0666 refid GNSS poll 3 noselect

Every second one sample goes to chrony, exactly as the dashboard would send it. Per second the
estimate error (sample offset; 0 with a perfect estimate, the clock being NTP time) is written to
seconds.csv; per minute the delay distribution, refused seconds, gaps, stamp rate and chrony's view
to minutes.csv. Disturbances: 10 min of full CPU load after 1 h, a driver restart after 2 h.
At the end summary.txt with the pass criteria.

    python3 tools/gnss_time_test.py --hours 8 --out ~/gnss-time-test
"""

import argparse
import math
import os
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aio_system_dashboard.collectors.gnss_time import ChronyShm, OffsetWindow   # noqa: E402


def chronyc(*args) -> str:
    try:
        return subprocess.run(["chronyc", "-c", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def pct(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, int(p / 100 * len(v)))] if v else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--out", default=str(Path.home() / "gnss-time-test"))
    ap.add_argument("--latency-ms", type=float, default=12.5)
    ap.add_argument("--topic", default="/openrtk330/imu/data_raw")
    ap.add_argument("--no-disturb", action="store_true")
    a = ap.parse_args()

    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Imu

    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    sec_f = open(out / "seconds.csv", "a", buffering=1)
    min_f = open(out / "minutes.csv", "a", buffering=1)
    ev_f = open(out / "events.txt", "a", buffering=1)
    if sec_f.tell() == 0:
        sec_f.write("utc,err_ms,median_delay_ms,messages,refused\n")
    if min_f.tell() == 0:
        min_f.write("utc,messages,delay_med_ms,delay_p5_ms,delay_p95_ms,delay_p99_ms,delay_max_ms,"
                    "err_min_ms,err_max_ms,err_over5ms_s,refused_s,gaps_50ms,stamp_back,rate_ppm,"
                    "shm_ok,chrony_sys_offset_ms,chrony_ref,gnss_stats\n")

    def event(text):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
        ev_f.write(line + "\n")
        print(line, flush=True)

    latency = a.latency_ms / 1000.0
    win = OffsetWindow(latency)
    shm = ChronyShm(2)
    shm_ok = shm.open()
    event(f"start: {a.hours} h, latency {a.latency_ms} ms, chrony SHM {'ok' if shm_ok else shm.error}")

    minute = {"delays": [], "errs": [], "refused": 0, "gaps": 0, "back": 0, "first": None, "last": None}
    state = {"prev": None}

    def on_imu(raw):
        now = time.time()
        sec, nsec = struct.unpack_from("<iI", raw, 4)
        stamp = sec + nsec * 1e-9
        win.add(now, stamp)
        prev = state["prev"]
        if prev is not None:
            if now - prev[0] > 0.05:
                minute["gaps"] += 1
            if stamp < prev[1]:
                minute["back"] += 1
        state["prev"] = (now, stamp)
        if minute["first"] is None:
            minute["first"] = (now, stamp)
        minute["last"] = (now, stamp)

    rclpy.init()
    node = Node("gnss_time_test")
    node.create_subscription(Imu, a.topic, on_imu, qos_profile_sensor_data, raw=True)

    t0 = time.monotonic()
    next_sec, next_min = t0 + 1.0, t0 + 60.0
    stress, stressed, restarted = [], False, False
    try:
        while time.monotonic() - t0 < a.hours * 3600:
            rclpy.spin_once(node, timeout_sec=0.05)
            mono = time.monotonic()
            if mono >= next_sec:
                next_sec += 1.0
                sample, why, delays = win.sample()
                utc = time.strftime("%H:%M:%S", time.gmtime())
                if sample is None:
                    minute["refused"] += 1
                    sec_f.write(f"{utc},,,0,{why}\n")
                else:
                    clock_t, recv_t = sample
                    err = (clock_t - recv_t) * 1000.0
                    if shm_ok:
                        shm.put(clock_t, recv_t)
                    minute["delays"] += delays
                    minute["errs"].append(err)
                    sec_f.write(f"{utc},{err:.3f},{statistics.median(delays) * 1000:.3f},{len(delays)},\n")
            if mono >= next_min:
                next_min += 60.0
                m = minute
                d = [x * 1000 for x in m["delays"]]
                e = m["errs"]
                rate = float("nan")
                if m["first"] and m["last"] and m["last"][0] > m["first"][0] + 10:
                    rate = ((m["last"][1] - m["first"][1]) / (m["last"][0] - m["first"][0]) - 1) * 1e6
                tr = chronyc("tracking").strip().split(",")
                sys_off = f"{float(tr[4]) * 1000:.3f}" if len(tr) > 4 else ""
                gnss = next((l for l in chronyc("sourcestats").splitlines() if l.startswith("GNSS")), "")
                min_f.write(",".join([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), str(len(d)),
                    *(f"{v:.3f}" for v in (pct(d, 50), pct(d, 5), pct(d, 95), pct(d, 99), max(d) if d else math.nan,
                                           min(e) if e else math.nan, max(e) if e else math.nan)),
                    str(sum(1 for x in e if abs(x) > 5)), str(m["refused"]), str(m["gaps"]), str(m["back"]),
                    f"{rate:.2f}", str(int(shm_ok)), sys_off, tr[1] if len(tr) > 1 else "",
                    '"' + gnss.replace('"', "") + '"']) + "\n")
                minute.update(delays=[], errs=[], refused=0, gaps=0, back=0, first=None, last=None)
                if not shm_ok:
                    shm_ok = shm.open()
            if not a.no_disturb:
                elapsed = mono - t0
                if not stressed and elapsed > 3600:
                    stressed = True
                    event("disturbance: full CPU load on all cores for 10 min")
                    stress = [subprocess.Popen(["timeout", "600", "python3", "-c", "while True: pass"])
                              for _ in range(os.cpu_count() or 4)]
                if not restarted and elapsed > 7200:
                    restarted = True
                    event("disturbance: restart of the sensor drivers")
                    subprocess.run(["systemctl", "--user", "restart", "openrtk330-basler-drivers.service"],
                                   timeout=90, check=False)
                    event("drivers restarted")
    except KeyboardInterrupt:
        event("interrupted")
    finally:
        for p in stress:
            p.kill()
    event("end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
