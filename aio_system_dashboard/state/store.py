"""Thread-safe latest-snapshot store and event log shared by collectors and web apps."""

import copy
import itertools
import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EVENT_LOG_MAX_BYTES = 5 * 1024 * 1024
# Before 2020 is not a real time: the Jetson keeps no time over a power-off, and each power-on
# starts at 1970 until GNSS time or NTP sets the clock.
CLOCK_VALID_FROM = 1577836800.0


class StateStore:
    """Sections are replaced whole by their single writer; readers get deep copies."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sections: Dict[str, Any] = {}
        self._updated: Dict[str, float] = {}

    def set(self, section: str, value: Any) -> None:
        with self._lock:
            self._sections[section] = value
            self._updated[section] = time.time()

    def get(self, section: str, default: Any = None) -> Any:
        with self._lock:
            if section not in self._sections:
                return copy.deepcopy(default)
            return copy.deepcopy(self._sections[section])

    def updated_at(self, section: str) -> Optional[float]:
        with self._lock:
            return self._updated.get(section)


class EventLog:
    """Recent events in memory plus an append-only JSONL file (one rotation)."""

    def __init__(self, log_file: Optional[Path], max_memory: int = 500, clock=time.time, mono=time.monotonic):
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=max_memory)
        self._ids = itertools.count(1)
        self._file = log_file
        self._clock, self._mono = clock, mono
        self._mono_of: Dict[int, float] = {}            # id -> monotonic time, for events stamped 1970
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)

    def add(self, level: str, category: str, title: str, detail: str = "", **extra) -> Dict:
        ts = self._clock()
        event = {
            "id": next(self._ids),
            "ts": ts,
            "level": level,          # info | success | warning | fault
            "category": category,    # health | action | diagnostic | ...
            "title": title,
            "detail": detail,
            **extra,
        }
        with self._lock:
            self._events.append(event)
            if ts < CLOCK_VALID_FROM:
                self._mono_of[event["id"]] = self._mono()
            self._write(event)
        log = logger.warning if level in ("warning", "fault") else logger.info
        log("event [%s/%s] %s %s", category, level, title, detail)
        return event

    def recent(self, limit: int = 100) -> List[Dict]:
        """Newest first. Events of a power-on's first minute were stamped 1970 (seen 2026-10-21: the
        Maintenance page said "08:00:40"); once the clock is set they get their real time, from the
        monotonic clock, which does not jump."""
        with self._lock:
            items = list(self._events)[-limit:]
            if self._mono_of and self._clock() >= CLOCK_VALID_FROM:
                offset = self._clock() - self._mono()
                for e in self._events:
                    m = self._mono_of.pop(e["id"], None)
                    if m is not None:
                        e["ts"] = m + offset
                self._mono_of.clear()                   # those that left the memory
        return list(reversed(items))

    def _write(self, event: Dict) -> None:
        if not self._file:
            return
        try:
            if self._file.exists() and self._file.stat().st_size > EVENT_LOG_MAX_BYTES:
                self._file.replace(self._file.with_suffix(self._file.suffix + ".1"))
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError as e:
            logger.warning("cannot write event log %s: %s", self._file, e)
