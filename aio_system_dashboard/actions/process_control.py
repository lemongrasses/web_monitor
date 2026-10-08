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

import json
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
    return [p for p in pgrep(pattern, max_age_s=0.0) if p != me] if pattern else []


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


def _wanted_file(cfg) -> Path:
    return cfg.state_file("processes.json")


def wanted(cfg, key: str) -> bool:
    """Should this program be running? True after a start, False after a stop (kept on disk, so
    it survives a dashboard restart). The DSO watchdog uses it to tell a crash from a stop."""
    try:
        return bool(json.loads(_wanted_file(cfg).read_text(encoding="utf-8")).get(key))
    except (OSError, ValueError, AttributeError):
        return False


def wanted_since(cfg, key: str) -> Optional[float]:
    """When the program was last asked to start (wall clock), if it should be running."""
    try:
        data = json.loads(_wanted_file(cfg).read_text(encoding="utf-8"))
        return float(data[f"{key}@"]) if data.get(key) and data.get(f"{key}@") else None
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return None


def set_wanted(cfg, key: str, value: bool, restamp: bool = True) -> None:
    """restamp=False: only change the flag (no new start time if it is already set)."""
    f = _wanted_file(cfg)
    try:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if bool(data.get(key)) == value and not (value and restamp):
            return
        data[key] = value
        if value:
            data[f"{key}@"] = time.time()           # every start request: tells a crash from "still starting"
        else:
            data.pop(f"{key}@", None)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("cannot record that %s should %srun: %s", key, "" if value else "not ", e)


_START_LOCKS: Dict[str, threading.Lock] = {}
_START_LOCKS_GUARD = threading.Lock()


def start_one(cfg, key: str, wait_s: float = 4.0, stable_s: float = 3.0) -> Dict:
    """Start one program unless it already runs. Only one start per program at a time: a manual
    Start and the DSO watchdog's retry can arrive together, and without this both would see "not
    running" and launch it twice. The second caller waits, then finds it running."""
    with _START_LOCKS_GUARD:
        lock = _START_LOCKS.setdefault(key, threading.Lock())
    with lock:
        return _start_one(cfg, key, wait_s, stable_s)


def _start_one(cfg, key: str, wait_s: float, stable_s: float) -> Dict:
    svc = cfg["services"][key]
    label = svc.get("label", key)
    set_wanted(cfg, key, True)
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
    for k in keys:
        set_wanted(cfg, k, False)
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
        for k in keys:
            set_wanted(cfg, k, verb in ("start", "restart"))
        if fake is not None:
            for k in keys:
                if verb in ("start", "restart"):
                    fake.set_service(k, "running")
                elif verb == "stop":
                    fake.set_service(k, "stopped")
        return {"success": True, "summary": f"(fake) {verb} {', '.join(keys)}"}

    notes: List[str] = []
    parts: List[Dict] = []     # per program, for callers that report only part of the group
    ok = True
    if verb in ("stop", "restart"):
        r = stop_many(cfg, keys, grace_s)
        ok &= r["ok"]
        notes.append(r["text"])
        parts += [{"key": k, "ok": r["ok"], "text": r["text"]} for k in keys]
        if not r["ok"]:
            return {"success": False, "summary": "; ".join(notes), "parts": parts}
        parts = []
    if verb in ("start", "restart"):
        for k in keys:
            r = start_one(cfg, k)
            ok &= r["ok"]
            notes.append(r["text"])
            parts.append({"key": k, "ok": r["ok"], "text": r["text"]})
    return {"success": ok, "summary": "; ".join(notes), "parts": parts}


def user_result(cfg, verb: str, result: Dict) -> Dict:
    """What the Overview page may show: AIO NAV only.

    The group also contains DSO, but the user view does not mention it. A DSO that fails to start
    does not make the Start look failed; its state is in Maintenance and the event log.
    """
    nav_key = cfg["nav"]["service"]
    label = cfg["services"].get(nav_key, {}).get("label", "AIO NAV")
    part = next((p for p in result.get("parts", []) if p["key"] == nav_key), None)
    ok = part["ok"] if part is not None else bool(result.get("success"))
    past = {"start": "started", "stop": "stopped", "restart": "restarted"}.get(verb, verb)
    if ok:
        text = part["text"] if part and "already" in part["text"] else f"{label} {past}"
    else:
        text = part["text"] if part else f"{label} could not be {past}"
        text = text.split(" (log: ")[0]
    return {"success": ok, "summary": text}
