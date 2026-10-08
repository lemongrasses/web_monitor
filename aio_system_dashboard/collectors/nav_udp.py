"""NAV UDP collector: receives aio_nav_node 0x04 packets at the raw rate.

Packet handling is decoupled from presentation: the listener thread only
updates in-memory state; ``status()`` is read by the 5 Hz health loop.
"""

import logging
import math
import socket
import threading
import time
from collections import deque
from typing import Dict, Optional

from ..config import parse_host_port
from ..nav.decoder import FLAG_NAMES, parse_nav_packet
from ..nav.trajectory import TrajectoryBuffer

logger = logging.getLogger(__name__)

RATE_WINDOW_S = 3.0  # long enough that a short gap does not read as "low rate"
TIME_BACKWARD_RESET_S = 1.0
STREAM_GAP_RESET_S = 1.0


class NavUdpCollector:
    name = "nav-udp"

    def __init__(self, bind: str, trajectory_cfg: Dict, on_session_reset=None):
        addr = parse_host_port(bind)
        if addr is None:
            raise ValueError(f"invalid nav.udp_bind: {bind!r}")
        self.bind_addr = addr
        self.trajectory = TrajectoryBuffer(**trajectory_cfg)
        self._on_session_reset = on_session_reset
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self.bind_error: Optional[str] = None
        self._latest: Optional[Dict] = None
        self._last_rx: Optional[float] = None
        self._rx_times: deque = deque()
        self._flag_last_seen: Dict[str, float] = {}
        self._packets = 0
        self._bad = 0
        self._last_bad_log = 0.0
        self._session_started: Optional[float] = time.time()

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _open(self) -> Optional[socket.socket]:
        # No SO_REUSEADDR: on Linux it would let a second listener share the UDP port
        # and silently split the packet stream between processes.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(self.bind_addr)
        except OSError as e:
            sock.close()
            self.bind_error = f"cannot bind {self.bind_addr[0]}:{self.bind_addr[1]}: {e}"
            return None
        sock.settimeout(0.5)
        self.bind_error = None
        logger.info("NAV UDP listening on %s:%d", *self.bind_addr)
        return sock

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                self._sock = self._open()
                if self._sock is None:
                    logger.error(self.bind_error)
                    self._stop.wait(5.0)
                    continue
            try:
                data, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                logger.exception("NAV UDP socket error; reopening")
                self._sock = None
                continue
            self.handle_datagram(data, time.monotonic())

    # ----------------------------------------------------------------- packets
    def handle_datagram(self, data: bytes, now: float) -> None:
        decoded = parse_nav_packet(data)
        if decoded is None:
            with self._lock:
                self._bad += 1
            if now - self._last_bad_log > 5.0:
                self._last_bad_log = now
                logger.warning("invalid NAV datagram len=%d head=%s", len(data), data[:8].hex())
            return

        reset = False
        with self._lock:
            prev = self._latest
            if prev is not None and decoded["time_s"] < prev["time_s"] - TIME_BACKWARD_RESET_S:
                reset = True
            self._latest = decoded
            if self._last_rx is not None and now - self._last_rx > STREAM_GAP_RESET_S:
                self._rx_times.clear()  # rate after a dropout reflects only the resumed stream
            self._last_rx = now
            if self._session_started is None:          # first packet after a stop: the session starts now
                self._session_started = time.time()
            self._packets += 1
            self._rx_times.append(now)
            for name in FLAG_NAMES:
                if decoded[name]:
                    self._flag_last_seen[name] = now
        if reset:
            self.new_session("NAV timestamp went backwards")
        self.trajectory.add(now, decoded["latitude"], decoded["longitude"])

    def new_session(self, reason: str) -> None:
        with self._lock:
            self._flag_last_seen.clear()
            self._session_started = time.time()
        session = self.trajectory.reset()
        logger.info("new NAV session %d (%s)", session, reason)
        if self._on_session_reset:
            self._on_session_reset(session, reason)

    def clear(self, reason: str) -> None:
        """AIO NAV stopped: forget its last solution, trajectory and flags. A restarted AIO NAV
        always starts from scratch, so nothing from the previous run should stay on screen."""
        with self._lock:
            self._latest = None
            self._last_rx = None
            self._rx_times.clear()
            self._flag_last_seen.clear()
            self._session_started = None
        session = self.trajectory.reset()
        logger.info("NAV state cleared, session %d (%s)", session, reason)
        if self._on_session_reset:
            self._on_session_reset(session, reason)

    def is_clear(self) -> bool:
        with self._lock:
            return self._latest is None and self._session_started is None and not self._flag_last_seen

    # ------------------------------------------------------------------ status
    def status(self, now: Optional[float] = None) -> Dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._rx_times and self._rx_times[0] < now - RATE_WINDOW_S:
                self._rx_times.popleft()
            # Divide by the observed span so the rate is right during the first seconds
            # of a stream, and still decays once packets stop.
            span = now - self._rx_times[0] if self._rx_times else RATE_WINDOW_S
            rate = len(self._rx_times) / max(span, 0.5)
            latest = dict(self._latest) if self._latest else None
            age = (now - self._last_rx) if self._last_rx is not None else None
            flag_age = {k: now - t for k, t in self._flag_last_seen.items()}
            out = {
                "listening": self.bind_error is None and self._sock is not None,
                "bind": f"{self.bind_addr[0]}:{self.bind_addr[1]}",
                "bind_error": self.bind_error,
                "packets": self._packets,
                "bad_packets": self._bad,
                "rate_hz": rate,
                "age_s": age,
                "session": self.trajectory.session,
                "session_started": self._session_started,
            }
        if latest:
            latest["speed"] = math.hypot(latest["velocity_north"], latest["velocity_east"])
        out["latest"] = latest
        out["flag_age_s"] = flag_age
        return out
