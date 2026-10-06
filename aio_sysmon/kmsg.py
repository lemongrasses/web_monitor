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


class KmsgReader:
    def __init__(self, on_event, keep: int = 400, path: str = "/dev/kmsg", max_per_min: int = 120):
        self.on_event, self.path, self.max_per_min = on_event, path, max_per_min
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
        self.recent.append((level, f"[{ts_us / 1e6:10.3f}] {msg}"))
        if (level <= 4 or IMPORTANT.search(msg)) and not NOISE.search(msg):
            now = time.monotonic()
            if now - self._window[0] > 60:
                self._window = [now, 0]
            self._window[1] += 1
            if self._window[1] <= self.max_per_min:             # a message storm must not fill the disk
                self.on_event("kernel", {"level": level, "seq": seq, "kt": round(ts_us / 1e6, 3), "msg": msg[:300]})

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
