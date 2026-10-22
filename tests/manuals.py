"""The manuals, for the tests that check them against the code, also where a sparse checkout keeps
docs/ out of the working tree (the handed-over device): those are read from git, never written."""

import fnmatch
import subprocess
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def kept_out() -> set:
    """The docs/ files in git that the sparse checkout leaves out (skip-worktree)."""
    return {line[2:] for line in _git("ls-files", "-t", "--", "docs").splitlines() if line.startswith("S ")}


def read(path: str) -> str:
    """The text of docs/..., from the working tree or, when the checkout leaves it out, from git."""
    f = ROOT / path
    if f.exists() or path not in kept_out():
        return f.read_text(encoding="utf-8")
    return _git("show", f":{path}")


def glob(pattern: str) -> List[str]:
    """The paths (from the repository root, sorted) matching pattern, e.g. "docs/*.zh-TW.md"."""
    found = {str(f.relative_to(ROOT)) for f in ROOT.glob(pattern)}
    return sorted(found | {p for p in kept_out() if fnmatch.fnmatch(p, pattern)})
