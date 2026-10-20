"""Minimal sd_notify: tell systemd the service is alive (WatchdogSec) without python-systemd."""

import os
import socket


def notify(message: str) -> bool:
    """Send one notification (e.g. "WATCHDOG=1"); False when not run by systemd or on error."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):                    # abstract namespace
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError:
        return False


def loops_alive(last_times, now: float, max_age_s: float = 20.0) -> bool:
    """Every loop (its last run, monotonic) ran within max_age_s; None = never ran."""
    return all(t is not None and now - t < max_age_s for t in last_times)


def watchdog_interval_s() -> float:
    """Half of WatchdogSec as systemd passes it (WATCHDOG_USEC), or 0 when there is none."""
    try:
        return int(os.environ.get("WATCHDOG_USEC", "0")) / 2e6
    except ValueError:
        return 0.0
