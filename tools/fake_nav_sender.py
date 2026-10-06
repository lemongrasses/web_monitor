#!/usr/bin/env python3
"""Fake aio_nav_node NAV UDP sender (0x04 packets) for dashboard development.

Scenarios (spec §37):
  normal        100 Hz, aligned, GNSS ok, vehicle driving a circle
  align         alignment false -> coarse (no heading) -> aligned -> fine
  gnss_loss     GNSS updates for 15 s, none for 15 s, repeat
  flags         cycles ZUPT / NHC / VUPT / ZIHR aiding flags
  pause         short 300 ms gaps every 5 s (UI must not flicker)
  loss          10 s streaming, 8 s total stream loss, repeat
  heading_wrap  heading oscillates 350..10 deg across north
  restart       timestamp jumps backwards every 30 s (new trajectory session)
  all           runs each scenario for --segment seconds in sequence

Example:  python3 tools/fake_nav_sender.py --scenario all
"""

import argparse
import math
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aio_system_dashboard.nav.decoder import encode_nav_packet, flags_to_int  # noqa: E402

ORIGIN = (22.99690, 120.22180)  # Tainan
EARTH_R = 6378137.0
SCENARIOS = ("normal", "align", "gnss_loss", "flags", "pause", "loss", "heading_wrap", "restart")


class Vehicle:
    """Drives a circle of ``radius`` m at ``speed`` m/s, clockwise (heading increases)."""

    def __init__(self, radius=80.0, speed=8.0):
        self.radius = radius
        self.speed = speed

    def at(self, t):
        w = self.speed / self.radius
        a = w * t
        north = self.radius * math.sin(a)
        east = self.radius * (1 - math.cos(a))
        vn = self.speed * math.cos(a)
        ve = self.speed * math.sin(a)
        heading = math.degrees(math.atan2(ve, vn)) % 360.0
        lat = ORIGIN[0] + math.degrees(north / EARTH_R)
        lon = ORIGIN[1] + math.degrees(east / (EARTH_R * math.cos(math.radians(ORIGIN[0]))))
        return lat, lon, vn, ve, heading


def frame(scenario, t, veh):
    """Return (send?, fields dict) for scenario time t (s since scenario start)."""
    lat, lon, vn, ve, heading = veh.at(t)
    flags = dict(alignment=True, heading_valid=True, fine_alignment=True,
                 nhc=True, gnss=(int(t * 100) % 100 == 0))  # 1 Hz GNSS update pulse
    send = True
    if scenario == "align":
        phase = t % 40
        flags.update(alignment=phase >= 10, heading_valid=phase >= 20, fine_alignment=phase >= 30)
        if phase < 20:
            vn = ve = 0.0
            lat, lon = ORIGIN
            flags.update(zupt=True, nhc=False)
    elif scenario == "gnss_loss":
        if t % 30 >= 15:
            flags["gnss"] = False
    elif scenario == "flags":
        step = int(t // 3) % 4
        flags.update(zupt=step == 0, nhc=step == 1, vupt=step == 2 and int(t * 100) % 10 == 0,
                     zihr=step == 3)
        if step in (0, 3):
            vn = ve = 0.0
    elif scenario == "pause":
        send = (t % 5) >= 0.3
    elif scenario == "loss":
        send = (t % 18) < 10
    elif scenario == "heading_wrap":
        heading = (20.0 * math.sin(t * 0.6)) % 360.0
        vn = 5.0 * math.cos(math.radians(heading))
        ve = 5.0 * math.sin(math.radians(heading))
    fields = dict(latitude=lat, longitude=lon, height=35.0 + math.sin(t * 0.1),
                  velocity_north=vn, velocity_east=ve, velocity_up=0.05 * math.sin(t),
                  roll=1.2 * math.sin(t * 0.7), pitch=-0.8 * math.cos(t * 0.5), heading=heading,
                  flags_c=flags_to_int(**flags),
                  stds=(0.05, 0.05, 0.1, 0.02, 0.02, 0.03, 0.05, 0.05, 0.3),
                  imu_cal=(1.5, -0.7, 2.1, 0.3, -0.2, 0.5, 120, -80, 40, 200, 150, -90))
    return send, fields


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="normal", choices=SCENARIOS + ("all",))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--rate", type=float, default=100.0)
    ap.add_argument("--segment", type=float, default=45.0, help="seconds per scenario in 'all'")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after N s (0 = forever)")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)
    veh = Vehicle()
    dt = 1.0 / args.rate
    t0 = time.monotonic()
    gps_sow0 = 300000.0  # GPS seconds-of-week style timestamp like aio_nav_node
    current = None
    n = 0
    next_tick = t0
    print(f"sending NAV packets to {dest[0]}:{dest[1]} at {args.rate:.0f} Hz, scenario={args.scenario}")
    try:
        while True:
            now = time.monotonic()
            t = now - t0
            if args.duration and t >= args.duration:
                break
            if args.scenario == "all":
                idx = int(t // args.segment) % len(SCENARIOS)
                scenario, st = SCENARIOS[idx], t % args.segment
            else:
                scenario, st = args.scenario, t
            if scenario != current:
                current = scenario
                print(f"[{t:7.1f}s] scenario: {scenario}", flush=True)
            nav_time = gps_sow0 + t
            if scenario == "restart":
                nav_time = gps_sow0 + (st % 30)  # jumps back every 30 s
            send, fields = frame(scenario, st, veh)
            if send:
                sock.sendto(encode_nav_packet(time_s=nav_time, **fields), dest)
                n += 1
            next_tick += dt
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        pass
    print(f"sent {n} packets")


if __name__ == "__main__":
    main()
