"""Multi-resolution trajectory buffer for one aio_nav_node session.

Recent points (last ``recent_window_s``) are kept at ``recent_hz``. When a
point ages out of the recent window it is promoted into the append-only
"older" list at most once per ``1/older_hz`` seconds. Clients fetch the older
list incrementally with a cursor and always receive the full recent window.
"""

import itertools
import threading
from collections import deque
from typing import Dict, List, Optional

_session_counter = itertools.count(1)


class TrajectoryBuffer:
    def __init__(
        self,
        recent_window_s: float = 60.0,
        recent_hz: float = 10.0,
        older_hz: float = 1.0,
        older_max_points: int = 36000,
    ):
        self.recent_window_s = recent_window_s
        self.recent_dt = 1.0 / recent_hz
        self.older_dt = 1.0 / older_hz
        self.older_max_points = older_max_points
        self._lock = threading.Lock()
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.session = next(_session_counter)
        self._recent: deque = deque()
        self._older: List[List[float]] = []
        self._older_offset = 0  # number of older points dropped from the front
        self._last_recent_slot: Optional[int] = None
        self._last_older_slot: Optional[int] = None

    def reset(self) -> int:
        with self._lock:
            self._reset_locked()
            return self.session

    def add(self, t: float, lat: float, lon: float) -> None:
        """Offer a sample at ``t`` (monotonic seconds). Decimated internally."""
        if not _valid_latlon(lat, lon):
            return
        with self._lock:
            # Time-slot bucketing is robust to jitter in the raw packet timing.
            slot = int(t // self.recent_dt)
            if slot == self._last_recent_slot:
                return
            self._last_recent_slot = slot
            self._recent.append((t, lat, lon))
            cutoff = t - self.recent_window_s
            while self._recent and self._recent[0][0] < cutoff:
                old_t, old_lat, old_lon = self._recent.popleft()
                older_slot = int(old_t // self.older_dt)
                if older_slot != self._last_older_slot:
                    self._last_older_slot = older_slot
                    self._older.append([round(old_lat, 8), round(old_lon, 8)])
            if len(self._older) > self.older_max_points:
                drop = len(self._older) - self.older_max_points
                del self._older[:drop]
                self._older_offset += drop

    def snapshot(self, session: Optional[int] = None, cursor: int = 0) -> Dict:
        """Return older points after ``cursor`` plus the full recent window.

        If ``session`` differs from the current session, or the cursor is no
        longer available, the client gets everything with ``reset: true``.
        """
        with self._lock:
            total = self._older_offset + len(self._older)
            reset = session != self.session or cursor < self._older_offset or cursor > total
            start = self._older_offset if reset else cursor
            older = self._older[start - self._older_offset:]
            return {
                "session": self.session,
                "reset": reset,
                "cursor": total,
                "older": older,
                "recent": [[round(la, 8), round(lo, 8)] for _, la, lo in self._recent],
            }


def _valid_latlon(lat: float, lon: float) -> bool:
    try:
        return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0)
    except TypeError:
        return False
