"""Keep the recorder able to run while the machine struggles, without costing anything the rest of
the time.

Normally it is an ordinary (slightly favoured) process. Only when a trigger shows that the machine
has stopped responding does it raise itself to a low real-time priority for a short while, so the
detailed sampling is not starved, and then it drops back. It never touches other programs.

Two guards keep the raise from becoming the problem: a watchdog thread that drops the priority if
the main loop stops making progress, and (in the service unit) LimitRTTIME, with which the kernel
itself kills a real-time task that computes for too long without sleeping.
"""

import ctypes
import os
import threading
from typing import Dict, Optional, Set

# what means "the machine has stopped responding" (plain high load or CPU use is not one of them)
DEFAULT_KINDS = ("stall", "iowait", "disk_wait_tasks", "memory_low", "swapping", "swap_high",
                 "hot", "slow_disk_write")

MCL_CURRENT, MCL_FUTURE, MCL_ONFAULT = 1, 2, 4   # ONFAULT: pin only pages actually used (not every mapped library)


def lock_memory() -> bool:
    """Keep the recorder's pages in RAM so memory pressure cannot swap it out. False if refused."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT) == 0
    except (OSError, AttributeError):
        return False


def locked_kb() -> Optional[int]:
    try:
        with open("/proc/self/smaps_rollup") as f:      # pages really pinned (VmLck counts whole areas)
            for line in f:
                if line.startswith("Locked:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


class Escalation:
    """Raise the main thread to SCHED_RR for a bounded time; always able to fall back."""

    def __init__(self, cfg: Dict, mono, set_policy=None):
        self.enabled = bool(cfg.get("escalate", True))
        self.kinds: Set[str] = set(cfg.get("escalate_on") or DEFAULT_KINDS)
        self.seconds = float(cfg.get("escalate_s", 60))
        self.rt_priority = int(cfg.get("escalate_priority", 10))
        self.stuck_s = float(cfg.get("escalate_stuck_s", 5))
        self.mono = mono
        self._set = set_policy or self._os_set
        self.until = 0.0
        self.active = False
        self.tid: Optional[int] = None
        self.heartbeat = mono()
        self.count = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _os_set(tid: int, rt_priority: int) -> None:
        if rt_priority > 0:
            os.sched_setscheduler(tid, os.SCHED_RR, os.sched_param(rt_priority))
        else:
            os.sched_setscheduler(tid, os.SCHED_OTHER, os.sched_param(0))

    def bind(self) -> None:
        """Call from the thread that will be raised."""
        self.tid = threading.get_native_id()
        if self.enabled and self._thread is None:
            self._thread = threading.Thread(target=self._watch, name="prio-guard", daemon=True)
            self._thread.start()

    def wants(self, kind: str) -> bool:
        return self.enabled and kind in self.kinds

    def raise_for(self, seconds: Optional[float] = None) -> Optional[bool]:
        """True: raised now; None: already raised, time extended; False: refused (no permission)."""
        if not self.enabled or self.tid is None:
            return False
        with self._lock:
            self.until = max(self.until, self.mono() + (seconds or self.seconds))
            if self.active:
                return None
            try:
                self._set(self.tid, self.rt_priority)
            except OSError:
                return False
            self.active = True
            self.count += 1
            return True

    def drop(self) -> bool:
        with self._lock:
            if not self.active or self.tid is None:
                return False
            try:
                self._set(self.tid, 0)
            except OSError:
                return False
            self.active = False
            return True

    def beat(self) -> None:
        """Called every loop; also ends a raise whose time has run out."""
        self.heartbeat = self.mono()
        if self.active and self.heartbeat >= self.until:
            self.drop()

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            if self.active and (self.mono() - self.heartbeat > self.stuck_s or self.mono() > self.until + self.stuck_s):
                self.drop()                      # the loop stopped making progress: back to normal

    def close(self) -> None:
        self._stop.set()
        self.drop()
