"""A fixed-size circular file of one-second records.

Disk use never grows (it is pre-allocated), the newest records overwrite the oldest, and the
file survives a power cut: after a freeze or reset the last hour or two, second by second, is
still there. A record is one JSON line padded to a fixed slot, so a half-written slot only
loses that one record.
"""

import json
import os
from typing import Dict, List, Optional

SLOT = 512


class RingFile:
    def __init__(self, path: str, size_bytes: int):
        self.path = path
        self.slots = max(16, size_bytes // SLOT)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
        want = self.slots * SLOT
        if os.fstat(self.fd).st_size != want:
            os.ftruncate(self.fd, want)
            try:
                os.posix_fallocate(self.fd, 0, want)       # reserve the blocks now: no ENOSPC later
            except (OSError, AttributeError):
                pass
        self.pos = self._next_slot()

    def _next_slot(self) -> int:
        """Continue after the newest record, so a restart does not scramble the order."""
        newest, idx = -1.0, -1
        for i, rec in enumerate(self._slots()):
            if rec is not None and rec.get("t", -1) >= newest:
                newest, idx = rec["t"], i
        return (idx + 1) % self.slots

    def _slots(self):
        os.lseek(self.fd, 0, os.SEEK_SET)
        data = b""
        while len(data) < self.slots * SLOT:
            chunk = os.read(self.fd, 1 << 20)
            if not chunk:
                break
            data += chunk
        for i in range(self.slots):
            raw = data[i * SLOT:(i + 1) * SLOT].rstrip(b"\0 \n")
            try:
                yield json.loads(raw) if raw else None
            except ValueError:
                yield None

    @staticmethod
    def encode(rec: Dict) -> Optional[bytes]:
        line = json.dumps(rec, separators=(",", ":")).encode()
        if len(line) > SLOT - 1:
            return None                                    # callers keep records lean; never half-write
        return line.ljust(SLOT - 1, b" ") + b"\n"

    def append_many(self, recs: List[Dict]) -> int:
        """Write records into consecutive slots (wrapping around). Returns how many were written."""
        blobs = [b for b in (self.encode(r) for r in recs) if b is not None]
        i = 0
        while i < len(blobs):
            n = min(len(blobs) - i, self.slots - self.pos)          # until the end of the file
            os.pwrite(self.fd, b"".join(blobs[i:i + n]), self.pos * SLOT)
            self.pos = (self.pos + n) % self.slots
            i += n
        return len(blobs)

    def append(self, rec: Dict) -> int:
        return self.append_many([rec])

    def sync(self) -> None:
        os.fdatasync(self.fd)

    def read_all(self) -> List[Dict]:
        recs = [r for r in self._slots() if r is not None]
        return sorted(recs, key=lambda r: r.get("t", 0))

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def read_ring(path: str) -> List[Dict]:
    """Read a ring file without opening it for writing (used by the report tool)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    out = []
    with open(path, "rb") as f:
        for _ in range(size // SLOT):
            raw = f.read(SLOT).rstrip(b"\0 \n")
            try:
                if raw:
                    out.append(json.loads(raw))
            except ValueError:
                continue
    return sorted(out, key=lambda r: r.get("t", 0))
