"""Where the records go, and the rules that keep the recorder small.

Everything lives in one directory with a hard size budget. Order of what is given up when space
is short: detailed snapshots first, then old daily samples, then old events. The disk guard
stops writing detail before the disk is nearly full, and below a critical level writes almost
nothing, so the recorder can never be the reason the disk fills up.
"""

import glob
import gzip
import json
import os
import shutil
import time
from typing import Dict, List, Optional

OK, LOW, CRITICAL = "ok", "low", "critical"


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


class Store:
    def __init__(self, cfg: Dict, clock=time.time, free_mb=None):
        self.cfg = cfg
        self.dir = cfg["dir"]
        self.clock = clock
        self._free_mb = free_mb or self._statvfs_free_mb
        for sub in ("samples", "snapshots"):
            os.makedirs(os.path.join(self.dir, sub), mode=0o755, exist_ok=True)
        self._files: Dict[str, object] = {}
        self.snapshots_today = 0
        self._snap_day = ""

    # ------------------------------------------------------------------ paths and guard
    def path(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    @staticmethod
    def _statvfs_free_mb(path: str) -> float:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1048576.0

    def free_mb(self) -> float:
        return self._free_mb(self.dir)

    def disk_level(self) -> str:
        free = self.free_mb()
        if free < float(self.cfg["critical_free_mb"]):
            return CRITICAL
        if free < float(self.cfg["min_free_mb"]):
            return LOW
        return OK

    def used_bytes(self) -> int:
        total = 0
        for root, _, files in os.walk(self.dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    # ------------------------------------------------------------------------ appending
    def _day(self, ts: float) -> str:
        return time.strftime("%Y%m%d", time.localtime(ts))

    def _append(self, path: str, rec: Dict) -> float:
        """Append one JSON line and make it durable. Returns the fsync time in ms."""
        f = self._files.get(path)
        if f is None:
            f = open(path, "ab", buffering=0)
            self._files[path] = f
        f.write((json.dumps(rec, separators=(",", ":")) + "\n").encode())
        t0 = time.monotonic()
        os.fsync(f.fileno())
        return (time.monotonic() - t0) * 1000.0

    def write_sample(self, rec: Dict) -> Optional[float]:
        if self.disk_level() == CRITICAL:        # almost no space: only the pre-allocated ring and tiny events
            return None
        path = self.path("samples", f"samples-{self._day(rec.get('t', self.clock()))}.jsonl")
        return self._append(path, rec)

    def write_event(self, kind: str, detail: Dict, ts: Optional[float] = None) -> None:
        ts = self.clock() if ts is None else ts
        rec = {"t": round(ts, 3), "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
               "kind": kind, **detail}
        self._append(self.path("events.jsonl"), rec)

    def write_last_known(self, rec: Dict) -> None:
        """Newest state, replaced atomically each time (survives a cut in the middle of a write)."""
        tmp = self.path("last_known.json.tmp")
        with open(tmp, "w") as f:
            json.dump(rec, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path("last_known.json"))

    def snapshot_allowed(self) -> bool:
        day = self._day(self.clock())
        if day != self._snap_day:
            self._snap_day, self.snapshots_today = day, 0
        return (self.disk_level() == OK and self.snapshots_today < int(self.cfg["max_snapshots_per_day"]))

    def write_snapshot(self, kind: str, data: Dict) -> Optional[str]:
        if not self.snapshot_allowed():
            return None
        ts = self.clock()
        name = f"snap-{time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))}-{kind}.json.gz"
        final = self.path("snapshots", name)
        tmp = final + ".tmp"
        with gzip.open(tmp, "wt", compresslevel=6) as f:
            json.dump(data, f, separators=(",", ":"))
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, final)
        # snapshots hold process command lines: readable by root and the adm group only
        try:
            os.chmod(final, 0o640)
            shutil.chown(final, group="adm")
        except (OSError, LookupError):
            pass
        _fsync_dir(os.path.dirname(final))
        self.snapshots_today += 1
        return final

    # ----------------------------------------------------------------------- the budget
    def enforce_budget(self) -> Dict:
        """Compress yesterday's files, then delete the oldest data until the size cap is met."""
        stats = {"compressed": 0, "deleted": 0}
        today = self._day(self.clock())
        for p in sorted(glob.glob(self.path("samples", "samples-*.jsonl"))):
            if today not in os.path.basename(p):
                self._close(p)
                with open(p, "rb") as src, gzip.open(p + ".gz", "wb", compresslevel=6) as dst:
                    shutil.copyfileobj(src, dst)
                os.remove(p)
                stats["compressed"] += 1
        # rotate a large events file
        ev = self.path("events.jsonl")
        if os.path.exists(ev) and os.path.getsize(ev) > float(self.cfg["events_max_mb"]) * 1048576:
            self._close(ev)
            name = self.path(f"events-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.clock()))}.jsonl.gz")
            with open(ev, "rb") as src, gzip.open(name, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(ev)
        cutoff = self.clock() - float(self.cfg["retention_days"]) * 86400
        for p in glob.glob(self.path("samples", "samples-*.gz")) + glob.glob(self.path("events-*.jsonl.gz")):
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                stats["deleted"] += 1
        cap = float(self.cfg["max_total_mb"]) * 1048576
        snap_cap = cap * float(self.cfg["snapshots_share"])
        stats["deleted"] += self._trim(glob.glob(self.path("snapshots", "snap-*.json.gz")), snap_cap)
        # still over the cap: give up the oldest daily samples, then old events
        for pattern in (self.path("samples", "samples-*.gz"), self.path("events-*.jsonl.gz")):
            while self.used_bytes() > cap:
                files = sorted(glob.glob(pattern), key=os.path.getmtime)
                if not files:
                    break
                os.remove(files[0])
                stats["deleted"] += 1
        return stats

    @staticmethod
    def _trim(files: List[str], cap_bytes: float) -> int:
        files = sorted(files, key=os.path.getmtime)
        total, n = sum(os.path.getsize(f) for f in files), 0
        while files and total > cap_bytes:
            f = files.pop(0)
            total -= os.path.getsize(f)
            os.remove(f)
            n += 1
        return n

    def _close(self, path: str) -> None:
        f = self._files.pop(path, None)
        if f:
            f.close()

    def close(self) -> None:
        for f in self._files.values():
            try:
                f.close()
            except OSError:
                pass
        self._files.clear()
