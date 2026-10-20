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
    """How often to tell systemd "alive": a sixth of WatchdogSec (WATCHDOG_USEC), at most 10 s, so
    a loop that stalled briefly and recovered pings well before the deadline; 0 = no watchdog."""
    try:
        usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    except ValueError:
        return 0.0
    return min(usec / 6e6, 10.0) if usec > 0 else 0.0
