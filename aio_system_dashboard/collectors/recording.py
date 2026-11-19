"""Is AIO NAV writing its logs? (the "Recording" step of the user pages)

AIO NAV writes its navigation log and its IMU / GNSS logs into one folder (fusion_txt_path, or
output/ next to aio-nav-ros: config.aio_nav_output_dir). Every poll looks at the files changed
last in that folder (and one level below): it is "writing" when one of them grew within
WRITING_WITHIN_S. The rate (bytes/s, over the last RATE_WINDOW_S) gives how long the free space
lasts. Only file sizes and times are read, never the contents.
"""

import os
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from .base import PeriodicCollector
from ..config import aio_nav_output_dir, resolve_path

WRITING_WITHIN_S = 5.0        # a file changed this recently: writing
PAUSE_S = 30.0                # not writing this long: the next write starts a new recording
RATE_WINDOW_S = 30.0
MAX_FILES = 2000              # a huge folder is not scanned whole


def scan(folder: str) -> Tuple[int, float, str]:
    """(total size of the files, newest modification time, newest file) of folder and its
    sub-folders one level down."""
    total, newest, newest_name = 0, 0.0, ""
    count = 0
    stack: List[Tuple[str, int]] = [(folder, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        for e in entries:
            count += 1
            if count > MAX_FILES:
                return total, newest, newest_name
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < 1:
                        stack.append((e.path, depth + 1))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            total += st.st_size
            if st.st_mtime > newest:
                newest, newest_name = st.st_mtime, os.path.relpath(e.path, folder)
    return total, newest, newest_name


class RecordingCollector(PeriodicCollector):
    name = "recording"
    section = "recording"

    def __init__(self, store, cfg, interval_s: float = 2.0, clock=time.monotonic, wall=time.time):
        super().__init__(store, interval_s)
        self.cfg, self._clock, self._wall = cfg, clock, wall
        self._sizes: deque = deque()             # (monotonic time, total size)
        self._since: Optional[float] = None      # start of the current recording (monotonic)
        self._last_write: Optional[float] = None

    def folder(self) -> Optional[str]:
        a = self.cfg.aio_nav
        out = aio_nav_output_dir(a.get("path") or "", a.get("fusion_txt_path") or "")
        if out:
            return out
        roots = self.cfg["data"]["roots"]
        return str(resolve_path(roots[0]["path"])) if roots else None

    def poll(self) -> Dict:
        now, wall = self._clock(), self._wall()
        a = self.cfg.aio_nav
        if a.get("save_fusion_txt") is False and a.get("save_parsed_txt") is False:
            self._reset()
            return {"available": True, "enabled": False, "writing": False,
                    "reason": "AIO NAV is set not to save logs (save_fusion_txt, save_parsed_txt)"}
        folder = self.folder()
        if not folder or not os.path.isdir(folder):
            self._reset()
            return {"available": False, "enabled": True, "writing": False, "folder": folder,
                    "reason": "The log folder of AIO NAV was not found"}
        total, newest, name = scan(folder)
        self._sizes.append((now, total))
        while self._sizes and now - self._sizes[0][0] > RATE_WINDOW_S:
            self._sizes.popleft()
        t0, s0 = self._sizes[0]
        rate = max(total - s0, 0) / (now - t0) if now - t0 >= 1.0 else None
        writing = newest > 0 and wall - newest <= WRITING_WITHIN_S
        if writing:
            if self._since is None or (self._last_write is not None and now - self._last_write > PAUSE_S):
                self._since = now
            self._last_write = now
        elif self._last_write is None or now - self._last_write > PAUSE_S:
            self._since = None
        return {"available": True, "enabled": True, "folder": folder, "file": name,
                "writing": writing, "rate_Bps": rate,
                "last_write_age_s": round(wall - newest, 1) if newest else None,
                "recording_s": round(now - self._since, 1) if self._since is not None else None}

    def _reset(self) -> None:
        self._sizes.clear()
        self._since = self._last_write = None
