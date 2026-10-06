"""Probes that measure lag directly instead of guessing it from CPU numbers."""

import threading
import time


class LatencyProbe:
    """Sleeps 100 ms in a loop and records how much later than that it woke up.

    When the machine is stuck or badly overloaded (the screen stops answering, a process is not
    scheduled), this is the number that shows it, even when CPU and load look ordinary."""

    def __init__(self, period_s: float = 0.1, mono=time.monotonic, sleep=time.sleep):
        self.period_s, self._mono, self._sleep = period_s, mono, sleep
        self._lock = threading.Lock()
        self._max_ms = 0.0
        self._stalls = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="latency-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = self._mono()
            self._sleep(self.period_s)
            self.record((self._mono() - t0 - self.period_s) * 1000.0)

    def record(self, late_ms: float) -> None:
        late_ms = max(0.0, late_ms)
        with self._lock:
            self._max_ms = max(self._max_ms, late_ms)
            if late_ms > 200:
                self._stalls += 1

    def take(self):
        """(worst lateness in ms, number of wake-ups later than 200 ms) since the last call."""
        with self._lock:
            out = (round(self._max_ms, 1), self._stalls)
            self._max_ms, self._stalls = 0.0, 0
        return out
