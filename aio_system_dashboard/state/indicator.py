"""Debounced status indicator.

Raw conditions are evaluated periodically; the displayed level only changes
after the new raw level has persisted for a hold time. Worsening uses
``raise_hold_s``, improving uses ``clear_hold_s`` (recovery must be stable
before the indicator returns to normal).
"""

from typing import Callable, Optional
import time

HEALTHY = "healthy"
WARNING = "warning"
FAULT = "fault"
UNKNOWN = "unknown"

_SEVERITY = {UNKNOWN: 0, HEALTHY: 1, WARNING: 2, FAULT: 3}


class DebouncedStatus:
    def __init__(
        self,
        raise_hold_s: float = 1.0,
        clear_hold_s: float = 2.0,
        initial: str = UNKNOWN,
        severity: Optional[Callable[[str], int]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.raise_hold_s = raise_hold_s
        self.clear_hold_s = clear_hold_s
        self.level = initial
        self.since = clock()
        self._severity = severity or (lambda lvl: _SEVERITY.get(lvl, 0))
        self._clock = clock
        self._pending: Optional[str] = None
        self._pending_since = 0.0

    def update(self, raw: str, now: Optional[float] = None) -> str:
        now = self._clock() if now is None else now
        if raw == self.level:
            self._pending = None
            return self.level
        if raw != self._pending:
            self._pending = raw
            self._pending_since = now
        # Leaving UNKNOWN happens immediately: there is nothing stable to protect.
        if self.level == UNKNOWN:
            hold = 0.0
        elif self._severity(raw) > self._severity(self.level):
            hold = self.raise_hold_s
        else:
            hold = self.clear_hold_s
        if now - self._pending_since >= hold:
            self.level = raw
            self.since = now
            self._pending = None
        return self.level
