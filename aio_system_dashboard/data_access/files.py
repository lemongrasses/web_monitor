"""Read-only access to approved data directories (spec §25).

Only configured roots are exposed. Every requested path is resolved with
realpath and must stay inside its root (rejects ``..``, absolute paths and
symlinks pointing outside). No upload, delete, rename or move.
"""

import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Optional

from ..config import resolve_path


class DataAccessError(Exception):
    def __init__(self, message: str, status: int = 404):
        super().__init__(message)
        self.status = status


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "data"


class DataRoots:
    def __init__(self, roots_cfg: List[Dict]):
        self.roots: Dict[str, Dict] = {}
        for r in roots_cfg:
            rid = r.get("id") or _slug(r.get("name", r["path"]))
            while rid in self.roots:
                rid += "-x"
            self.roots[rid] = {"id": rid, "name": r.get("name", rid),
                               "path": str(resolve_path(r["path"])),
                               "description": r.get("description", "")}

    def list_roots(self) -> List[Dict]:
        out = []
        for r in self.roots.values():
            out.append({**r, "exists": os.path.isdir(r["path"])})
        return out

    def resolve(self, root_id: str, rel: str = "") -> Path:
        root = self.roots.get(root_id)
        if root is None:
            raise DataAccessError("unknown data location")
        rel = (rel or "").replace("\\", "/")
        if rel.startswith("/") or "\x00" in rel:
            raise DataAccessError("invalid path", 400)
        base = os.path.realpath(root["path"])
        target = os.path.realpath(os.path.join(base, rel))
        if os.path.commonpath([base, target]) != base:
            raise DataAccessError("path outside data location", 403)
        if not os.path.exists(target):
            raise DataAccessError("not found")
        return Path(target)

    def relpath(self, root_id: str, p: Path) -> str:
        base = os.path.realpath(self.roots[root_id]["path"])
        rel = os.path.relpath(str(p), base)
        return "" if rel == "." else rel

    def listing(self, root_id: str, rel: str = "") -> Dict:
        target = self.resolve(root_id, rel)
        if not target.is_dir():
            raise DataAccessError("not a directory", 400)
        cur = self.relpath(root_id, target)
        entries = []
        with os.scandir(target) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                logical = f"{cur}/{e.name}" if cur else e.name
                try:
                    st = self.resolve(root_id, logical).stat()
                except (DataAccessError, OSError):
                    continue  # skip broken or escaping symlinks
                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({"name": e.name, "path": logical, "is_dir": is_dir,
                                "size": None if is_dir else st.st_size, "mtime": st.st_mtime})
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return {
            "root": self.roots[root_id],
            "path": cur,
            "parent": None if cur == "" else os.path.dirname(cur),
            "breadcrumbs": _breadcrumbs(cur),
            "entries": entries,
        }

    def file_for_download(self, root_id: str, rel: str) -> Path:
        target = self.resolve(root_id, rel)
        if not target.is_file():
            raise DataAccessError("not a file", 400)
        return target


def _breadcrumbs(rel: str) -> List[Dict]:
    crumbs: List[Dict] = []
    acc: Optional[str] = None
    for part in [p for p in rel.split("/") if p]:
        acc = part if acc is None else f"{acc}/{part}"
        crumbs.append({"name": part, "path": acc})
    return crumbs
