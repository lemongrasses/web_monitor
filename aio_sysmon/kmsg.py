"""Copies kernel warnings and errors to disk as they happen (needs root: /dev/kmsg).

The kernel's own log lives in RAM and is gone after a power cut. Out-of-memory kills, disk
timeouts, thermal throttling, GPU errors and "task blocked" messages are exactly what explains a
freeze, so they are written to the event log immediately.
"""

import os
import re
import select
import threading
import time
from collections import deque
from typing import Deque, Tuple

IMPORTANT = re.compile(
    r"out of memory|oom-kill|oom_reaper|killed process|hung task|soft lockup|hard lockup|rcu.*stall|"
    r"blocked for more than|call trace|\bbug:|kernel panic|watchdog|thermal|throttl|over-?temp|"
    r"over-?curr|under-?volt|brown-?out|nvme.*(timeout|reset|abort|error)|i/o error|ext4-fs (error|warning)|"
    r"nvgpu.*(fail|error|timeout)|nvmap.*fail|nvrm|xid|serror|segfault|tegra.*(error|fail)|"
    r"usb.*(disconnect|reset)|link is down|mmc.*error|firmware.*fail|reset reason|brownout",
    re.I)

# Boot-time chatter that mentions the same words but means nothing is wrong.
NOISE = re.compile(r"cooling device registered|thermal-trip-event .*registered|trip.*registered", re.I)


def parse(line: str):
    """'6,339,5140900,-;message' -> (level, seq, ts_us, message), or None for continuation lines."""
    if not line or line[0] == " ":
        return None
    head, _, msg = line.partition(";")
    f = head.split(",")
    if len(f) < 3:
        return None
    try:
        return int(f[0]) & 7, int(f[1]), int(f[2]), msg.rstrip("\n")
    except ValueError:
        return None


def _normalise(msg: str) -> str:
    """The message without process ids and numbers, so 'the same thing again' is recognised."""
    return re.sub(r"\d+", "N", msg)[:160]


class KmsgReader:
    def __init__(self, on_event, keep: int = 400, path: str = "/dev/kmsg", max_per_min: int = 120,
                 min_seq: int = 0, repeat_window_s: float = 600.0, clock=time.monotonic):
        self.on_event, self.path, self.max_per_min = on_event, path, max_per_min
        self.min_seq = min_seq                    # messages up to this sequence number were already recorded
        self.last_seq = min_seq
        self.repeat_window_s, self._clock = repeat_window_s, clock
        self._seen: dict = {}                     # normalised text -> [first time, suppressed count, first message]
        self.recent: Deque[Tuple[int, str]] = deque(maxlen=keep)
        self._stop = threading.Event()
        self._thread = None
        self.available = False
        self._window = [0.0, 0]

    def start(self) -> bool:
        try:
            self._fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return False
        self.available = True
        self._thread = threading.Thread(target=self._run, name="kmsg", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def handle(self, line: str) -> None:
        p = parse(line)
        if not p:
            return
        level, seq, ts_us, msg = p
        msg = msg.split("\n", 1)[0]                              # the first line; the rest is key=value detail
        self.last_seq = max(self.last_seq, seq)
        if seq <= self.min_seq:                                  # recorded before the recorder restarted
            return
        self.recent.append((level, f"[{ts_us / 1e6:10.3f}] {msg}"))
        if (level <= 4 or IMPORTANT.search(msg)) and not NOISE.search(msg):
            now = self._clock()
            key = _normalise(msg)
            seen = self._seen.get(key)
            if seen and now - seen[0] < self.repeat_window_s:    # the same message again: count it, do not log it
                seen[1] += 1
                return
            if seen and seen[1]:
                self._emit_repeats(key, seen)
            self._seen[key] = [now, 0, msg]
            if now - self._window[0] > 60:
                self._window = [now, 0]
            self._window[1] += 1
            if self._window[1] <= self.max_per_min:             # a message storm must not fill the disk
                self.on_event("kernel", {"level": level, "seq": seq, "kt": round(ts_us / 1e6, 3), "msg": msg[:300]})

    def _emit_repeats(self, key: str, seen) -> None:
        self.on_event("kernel_repeats", {"msg": seen[2][:200], "count": seen[1],
                                         "window_s": round(self._clock() - seen[0])})
        seen[1] = 0

    def flush_repeats(self) -> None:
        """Write the 'N more times' summaries (called when stopping)."""
        for key, seen in list(self._seen.items()):
            if seen[1]:
                self._emit_repeats(key, seen)

    def tail(self, n: int = 40):
        return [m for _, m in list(self.recent)[-n:]]

    def _run(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([self._fd], [], [], 1.0)
            if not r:
                continue
            try:
                while True:
                    self.handle(os.read(self._fd, 8192).decode(errors="replace"))
            except BlockingIOError:
                continue
            except OSError:
                time.sleep(0.5)
