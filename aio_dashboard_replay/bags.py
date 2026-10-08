"""Find rosbag2 recordings in the configured folders and read what is in them (metadata.yaml).

A folder that holds a ``metadata.yaml`` is a bag; the search does not go into a bag, skips hidden
folders and stops at ``scan_depth`` levels below each root. Only bags found here can be played:
the web page refers to a bag by its id, never by a path it chose itself.
"""

import hashlib
import os
import threading
import time
from typing import Dict, List, Optional

import yaml

MAX_BAGS = 300


def bag_id(path: str) -> str:
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:12]


def _ns(value) -> int:
    try:
        if isinstance(value, dict):       # duration: {nanoseconds}, starting_time: {nanoseconds_since_epoch}
            value = value.get("nanoseconds", value.get("nanoseconds_since_epoch", 0))
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_metadata(path: str) -> Optional[Dict]:
    """Summary of one bag folder, or None if its metadata.yaml cannot be read."""
    try:
        with open(os.path.join(path, "metadata.yaml"), encoding="utf-8") as f:
            info = (yaml.safe_load(f) or {}).get("rosbag2_bagfile_information") or {}
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    topics = []
    for t in info.get("topics_with_message_count") or []:
        meta = t.get("topic_metadata") or {}
        if meta.get("name"):
            topics.append({"name": meta["name"], "type": meta.get("type", ""),
                           "count": int(t.get("message_count") or 0)})
    duration_s = _ns(info.get("duration")) / 1e9
    for t in topics:
        t["hz"] = round(t["count"] / duration_s, 1) if duration_s > 0 else None
    topics.sort(key=lambda t: t["name"])
    size = 0
    for name in info.get("relative_file_paths") or []:
        try:
            size += os.path.getsize(os.path.join(path, name))
        except OSError:
            pass
    return {
        "id": bag_id(path),
        "name": os.path.basename(os.path.normpath(path)),
        "path": os.path.realpath(path),
        "storage": info.get("storage_identifier", ""),
        "duration_s": round(duration_s, 3),
        "start_ns": _ns(info.get("starting_time")),
        "messages": int(info.get("message_count") or 0),
        "size": size,
        "topics": topics,
    }


def find(roots: List[str], depth: int = 2) -> List[Dict]:
    """Every bag under the roots (summaries without the topic list), newest first."""
    seen, out = set(), []

    def walk(d: str, level: int) -> None:
        if len(out) >= MAX_BAGS:
            return
        if os.path.isfile(os.path.join(d, "metadata.yaml")):
            real = os.path.realpath(d)
            if real not in seen:
                seen.add(real)
                meta = read_metadata(d)
                if meta:
                    out.append(meta)
            return
        if level >= depth:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if not e.name.startswith(".") and e.is_dir(follow_symlinks=True):
                walk(e.path, level + 1)

    for root in roots:
        root = os.path.expanduser(str(root))
        if os.path.isdir(root):
            walk(root, 0)
    out.sort(key=lambda b: b["start_ns"], reverse=True)
    return out


class BagIndex:
    """`find` with a short cache, so the page can ask often without rescanning the disk."""

    def __init__(self, roots: List[str], depth: int, max_age_s: float = 15.0):
        self.roots, self.depth, self.max_age_s = roots, depth, max_age_s
        self._lock = threading.Lock()
        self._bags: List[Dict] = []
        self._at = 0.0

    def all(self, refresh: bool = False) -> List[Dict]:
        with self._lock:
            if refresh or time.monotonic() - self._at > self.max_age_s:
                self._bags = find(self.roots, self.depth)
                self._at = time.monotonic()
            return self._bags

    def get(self, bid: str) -> Optional[Dict]:
        for refresh in (False, True):
            for b in self.all(refresh):
                if b["id"] == bid:
                    return b
        return None
