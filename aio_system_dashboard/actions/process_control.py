"""Start / stop AIO NAV and DSO the way the AIO Nav desktop app (aio-nav-ui) does.

* Start: for each member, spawn its wrapper (aio-nav, aio-nav-dso from the aio-nav-ros
  install/ folder) in its own session unless a process matching its pattern already runs
  (so it never doubles a node started from the app).
* Stop: SIGTERM every matching process, then SIGKILL what is left after a grace period.
* Processes are found by command-line pattern, so ones started by the app are controlled too.

Targets come from config only (``services.<key>.launch``); the web request never carries a
command. The children get a clean environment and the active mode's aio-nav-ros config file
(aio_nav.yaml for live, aio_nav_bag.yaml for bag replay) as their argument; that file decides
ros_domain_id, ros_localhost_only and use_sim_time.
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
from ..config import aio_nav_output_dir, resolve_path

logger = logging.getLogger(__name__)

KEEP_ENV = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "SHELL", "DISPLAY",
            "XAUTHORITY", "XDG_RUNTIME_DIR")
GROUP = "nav_core"


def child_env(cfg=None) -> Dict[str, str]:
    env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
    if cfg is not None:
        env.update(cfg.ros_env())  # else the wrappers read ros_domain_id from aio_nav.yaml
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


def running_pids(cfg, key: str) -> List[int]:
    """PIDs of the process behind a service (matched by its process_pattern)."""
    return _pids(cfg, key)


def _pids(cfg, key: str) -> List[int]:
    pattern = cfg["services"][key].get("process_pattern")
    me = os.getpid()
    return [p for p in pgrep(pattern) if p != me] if pattern else []


def _reap(proc: subprocess.Popen) -> None:
    threading.Thread(target=proc.wait, daemon=True, name="reap").start()


ERROR_WORDS = ("assertion", "aborted", "error", "traceback", "terminate", "segmentation", "fatal")


def _tail(path: Path, n: int = 3) -> str:
    """The most telling lines of a program's log: error lines if there are any, else the last ones."""
    try:
        lines = [l.strip() for l in path.read_text(errors="replace").splitlines() if l.strip()][-60:]
    except OSError:
        return ""
    errors = [l for l in lines if any(w in l.lower() for w in ERROR_WORDS)]
    return " | ".join((errors or lines)[-n:])


def work_dir(cfg) -> str:
    """Folder the programs run in. AIO NAV writes its logs to a relative output/, so this is the
    folder that holds output/ (next to install/), where the Data page looks."""
    out = aio_nav_output_dir(cfg.aio_nav.get("path", ""), cfg.aio_nav.get("fusion_txt_path", ""))
    if out and os.path.isdir(os.path.dirname(out)):
        return os.path.dirname(out)
    return str(Path.home())


def start_one(cfg, key: str, wait_s: float = 4.0, stable_s: float = 3.0) -> Dict:
    svc = cfg["services"][key]
    label = svc.get("label", key)
    if _pids(cfg, key):
        return {"ok": True, "text": f"{label} already running"}
    launcher = cfg.launcher(svc["launch"])
    if not launcher:
        return {"ok": False, "text": f"{label}: launcher '{svc['launch']}' not found "
                                     "(aio-nav-ros install folder)"}
    argv = [launcher] + ([cfg.mode_config_path()] if cfg.mode_config_path() else [])
    log = resolve_path(f"logs/{key}.log")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as out:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    env=child_env(cfg), cwd=work_dir(cfg))
    except OSError as e:
        return {"ok": False, "text": f"{label}: cannot start ({e})"}
    _reap(proc)
    deadline = time.monotonic() + wait_s
    seen = False
    while time.monotonic() < deadline:
        if _pids(cfg, key):
            seen = True
            break
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    if seen:  # a program that dies right after launching is not "started"
        time.sleep(stable_s)
        time.sleep(0.3)  # let a crashing program finish writing its log
        if _pids(cfg, key):
            return {"ok": True, "text": f"{label} started"}
    detail = _tail(log)
    what = "exited right after starting" if seen else "did not start"
    return {"ok": False, "text": f"{label} {what}" + (f": {detail}" if detail else "")
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
