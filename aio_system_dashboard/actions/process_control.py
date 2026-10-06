"""Start / stop AIO NAV and DSO the way the AIO Nav desktop app (aio-nav-ui) does.

* Start: for each member, spawn its wrapper (aio-nav, aio-nav-dso from the aio-nav-ros
  install/ folder) in its own session unless a process matching its pattern already runs
  (so it never doubles a node started from the app).
* Stop: SIGTERM every matching process, then SIGKILL what is left after a grace period.
* Processes are found by command-line pattern, so ones started by the app are controlled too.

Targets come from config only (``services.<key>.launch``); the web request never carries a
command. The children get a clean environment so ROS_DOMAIN_ID / ROS_LOCALHOST_ONLY come from
aio_nav.yaml (the wrappers read them there), not from the dashboard's own ROS settings.
"""

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..collectors.services import pgrep
from ..config import resolve_path

logger = logging.getLogger(__name__)

KEEP_ENV = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "SHELL", "DISPLAY",
            "XAUTHORITY", "XDG_RUNTIME_DIR")
GROUP = "nav_core"


def child_env() -> Dict[str, str]:
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    env.setdefault("DISPLAY", ":0")  # same default as the desktop app
    return env


def members(cfg) -> List[str]:
    """Services of the Overview Start/Stop group, in start order."""
    return [k for k in cfg["nav"]["control_group"]
            if (cfg["services"].get(k) or {}).get("launch")]


def targets(cfg) -> Dict[str, str]:
    out = {k: s.get("label", k) for k, s in cfg["services"].items() if s.get("launch")}
    if members(cfg):
        out[GROUP] = cfg["services"][cfg["nav"]["service"]].get("label", "AIO NAV")
    return out


def _keys(cfg, target: str) -> List[str]:
    return members(cfg) if target == GROUP else [target]


def _pids(cfg, key: str) -> List[int]:
    pattern = cfg["services"][key].get("process_pattern")
    me = os.getpid()
    return [p for p in pgrep(pattern) if p != me] if pattern else []


def _reap(proc: subprocess.Popen) -> None:
    threading.Thread(target=proc.wait, daemon=True, name="reap").start()


def _tail(path: Path, n: int = 3) -> str:
    try:
        lines = [l.strip() for l in path.read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return ""
    return " | ".join(lines[-n:])


def start_one(cfg, key: str, wait_s: float = 4.0) -> Dict:
    svc = cfg["services"][key]
    label = svc.get("label", key)
    if _pids(cfg, key):
        return {"ok": True, "text": f"{label} already running"}
    launcher = cfg.launcher(svc["launch"])
    if not launcher:
        return {"ok": False, "text": f"{label}: launcher '{svc['launch']}' not found "
                                     "(aio-nav-ros install folder)"}
    log = resolve_path(f"logs/{key}.log")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as out:
            proc = subprocess.Popen([launcher], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    env=child_env(), cwd=str(Path.home()))
    except OSError as e:
        return {"ok": False, "text": f"{label}: cannot start ({e})"}
    _reap(proc)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _pids(cfg, key):
            return {"ok": True, "text": f"{label} started"}
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    if _pids(cfg, key):
        return {"ok": True, "text": f"{label} started"}
    detail = _tail(log)
    return {"ok": False, "text": f"{label} did not start" + (f": {detail}" if detail else "")
                                 + f" (log: {log})"}


def stop_many(cfg, keys: List[str], grace_s: float) -> Dict:
    pids = {k: _pids(cfg, k) for k in keys}
    allp = [p for v in pids.values() for p in v]
    if not allp:
        return {"ok": True, "text": "already stopped"}
    for p in allp:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and any(_alive(p) for p in allp):
        time.sleep(0.2)
    killed = 0
    for p in allp:
        if _alive(p):
            try:
                os.kill(p, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
    time.sleep(0.2)
    left = [p for p in allp if _alive(p)]
    text = f"stopped {len(allp)} process(es)" + (f", {killed} had to be killed" if killed else "")
    return {"ok": not left, "text": text if not left else f"could not stop pid {left}"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is gone as far as we care
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def control(cfg, target: str, verb: str, grace_s: float, fake=None) -> Dict:
    keys = _keys(cfg, target)
    if cfg.fake:
        time.sleep(1.5)
        if fake is not None:
            for k in keys:
                if verb in ("start", "restart"):
                    fake.set_service(k, "running")
                elif verb == "stop":
                    fake.set_service(k, "stopped")
        return {"success": True, "summary": f"(fake) {verb} {', '.join(keys)}"}

    notes: List[str] = []
    ok = True
    if verb in ("stop", "restart"):
        r = stop_many(cfg, keys, grace_s)
        ok &= r["ok"]
        notes.append(r["text"])
        if not r["ok"]:
            return {"success": False, "summary": "; ".join(notes)}
    if verb in ("start", "restart"):
        for k in keys:
            r = start_one(cfg, k)
            ok &= r["ok"]
            notes.append(r["text"])
    return {"success": ok, "summary": "; ".join(notes)}
