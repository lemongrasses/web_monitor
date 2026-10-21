#!/usr/bin/env python3
"""Reference vehicle-side receiver: listen where the vehicle listens, decode 0x04 navigation
and 0x05 status packets (both arrive on the output_udp port by default) and print when
navigation becomes usable or blocked.

    python3 tools/vehicle_gate.py --port 9000                  # 0x04 and 0x05 on 9000
    python3 tools/vehicle_gate.py --port 9000 --status-port 9001   # 0x05 sent elsewhere
    python3 tools/vehicle_gate.py --port 9000 --verbose        # also every status packet

The rules are in aio_system_dashboard/nav/vehicle_gate.py (docs/OPERATION.md, "Status packet").
Exit with Ctrl+C; a summary of packets, losses and time spent usable is printed.
"""

import argparse
import select
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aio_system_dashboard.nav.vehicle_gate import VehicleGate      # noqa: E402
from aio_system_dashboard.state.readiness import ACTIONS, STATES    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000, help="0x04 navigation port (output_udp)")
    ap.add_argument("--status-port", type=int, default=None, help="0x05 status port (default: the same as --port)")
    ap.add_argument("--status-id", type=lambda x: int(x, 0), default=0x05)
    ap.add_argument("--from", dest="allowed_from", default=None,
                    help="accept packets only from this sender IP (the dashboard computer)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    ports = sorted({a.port, a.status_port if a.status_port is not None else a.port})
    socks = []
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((a.host, port))
        socks.append(sock)
    gate = VehicleGate(status_message_id=a.status_id, allowed_from=a.allowed_from)
    t0 = time.monotonic()
    last = None
    usable_s, counts = 0.0, {"nav": 0, "status": 0}
    prev_t = t0
    print(f"listening on {a.host} ports {', '.join(map(str, ports))} (status id 0x{a.status_id:02x})", flush=True)
    try:
        while True:
            ready, _, _ = select.select(socks, [], [], 0.1)
            now = time.monotonic()
            for sock in ready:
                data, sender = sock.recvfrom(2048)
                kind = gate.feed(data, now, sender[0])
                if kind:
                    counts[kind] += 1
                if kind == "status" and a.verbose:
                    s = gate.status
                    print(f"  status seq={s['sequence']} {STATES[s['state']] if s['state'] < len(STATES) else s['state']} "
                          f"action={ACTIONS[s['requested_action']] if s['requested_action'] < len(ACTIONS) else '?'} "
                          f"inhibit=0x{s['inhibit_flags']:05x}", flush=True)
            ok, why = gate.allowed(now)
            if ok:
                usable_s += now - prev_t
            prev_t = now
            if (ok, why) != last:
                state = STATES[gate.status["state"]] if gate.status and gate.status["state"] < len(STATES) else "-"
                print(f"[{now - t0:7.1f}s] {'USABLE ' if ok else 'BLOCKED'} {why}  (status {state})", flush=True)
                last = (ok, why)
    except KeyboardInterrupt:
        pass
    print(f"\nnav packets {counts['nav']}, status packets {counts['status']}, lost status {gate.lost}, "
          f"reordered {gate.reordered}, invalid {gate.bad}, two senders {gate.conflicts}, "
          f"other senders {gate.foreign}, usable {usable_s:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
