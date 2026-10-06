"""Periodic background collector. Collectors only read system state."""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class PeriodicCollector:
    name = "collector"
    section: Optional[str] = None  # store section written by poll()

    def __init__(self, store, interval_s: float):
        self.store = store
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.poll()
                if self.section and result is not None:
                    self.store.set(self.section, result)
            except Exception:  # keep long-running collectors alive
                logger.exception("%s poll failed", self.name)
            self._stop.wait(self.interval_s)

    def poll(self):
        raise NotImplementedError
