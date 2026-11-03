"""systemd unit status for configured services (plus process fallback)."""

import os
import re
import subprocess
import time
from typing import Dict, List, Optional

from .base import PeriodicCollector


def user_bus_env() -> Dict[str, str]:
    """Environment `systemctl --user` needs when run from a system service (no login session)."""
    env = dict(os.environ)
    run = f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", run)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run}/bus")
    return env


def systemd_unit_state(unit: str, timeout: float = 3.0, user: bool = False) -> Dict:
    try:
        out = subprocess.run(
            ["systemctl"] + (["--user"] if user else []) + ["show", unit, "--no-pager",
             "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
             "-p", "MainPID", "-p", "ActiveEnterTimestamp", "-p", "NRestarts",
             "-p", "UnitFileState"],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=user_bus_env() if user else None,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"load": "unknown", "active": "unknown", "error": str(e)}
    props = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    return {
        "load": props.get("LoadState", "unknown"),
        "active": props.get("ActiveState", "unknown"),
        "sub": props.get("SubState", ""),
        "main_pid": int(props.get("MainPID") or 0),
        "since": props.get("ActiveEnterTimestamp", ""),
        "restarts": props.get("NRestarts", ""),
        "file_state": props.get("UnitFileState", ""),      # enabled = started by systemd at boot
    }


_PROC_CACHE: Dict = {"t": 0.0, "procs": []}


def _command_lines(max_age_s: float = 1.0) -> List:
    """(pid, command line) of every process, read from /proc at most once per max_age_s.

    Replaces `pgrep -f`: each call used to start a child process, and the services collector asks
    for several patterns every two seconds."""
    now = time.monotonic()
    if now - _PROC_CACHE["t"] < max_age_s and _PROC_CACHE["procs"]:
        return _PROC_CACHE["procs"]
    procs = []
    me = os.getpid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            continue            # the process ended while we were looking
        if raw:                 # kernel threads have no command line
            procs.append((int(name), raw.replace(b"\0", b" ").decode(errors="replace").strip()))
    _PROC_CACHE["t"], _PROC_CACHE["procs"] = now, procs
    return procs


def pgrep(pattern: str, timeout: float = 2.0, max_age_s: float = 1.0) -> List[int]:
    """PIDs whose command line matches the regular expression (like `pgrep -f`).

    max_age_s: how old the shared /proc scan may be. Use 0 when a decision depends on it (start /
    stop), so a process that just started or ended is seen."""
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    return [pid for pid, cmd in _command_lines(max_age_s) if rx.search(cmd)]


# A unit whose restart count rose this recently is in a crash loop: it is reported as failed
# even while systemd briefly shows it active again (Restart= brings it back every RestartSec).
CRASH_LOOP_WINDOW_S = 60.0
_SYSTEMD_OWN_LINE = re.compile(r"^(Started |Starting |Stopped |Stopping |Scheduled restart|Consumed |Deactivated )"
                               r"|: (Main process exited|Failed with result|Scheduled restart|Consumed )")


def unit_last_message(unit: str, user: bool = False, timeout: float = 2.0) -> str:
    """The last line the unit's own program logged (journal), skipping systemd's lifecycle lines:
    the reason a crash-looping driver gives ("/dev/ttyUSB0 not present"). "" when unreadable."""
    try:
        out = subprocess.run(
            ["journalctl"] + (["--user"] if user else []) + ["-u", unit, "-n", "12", "-o", "cat", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=timeout, check=False,
            env=user_bus_env() if user else None)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    own = [l for l in lines if not _SYSTEMD_OWN_LINE.search(l)]
    return (own or lines or [""])[-1][:200]


def summarize(unit_state: Dict, pids: Optional[List[int]]) -> str:
    """running | stopped | failed | not_installed | unknown."""
    active = unit_state.get("active")
    if unit_state.get("crash_loop"):
        return "failed"   # keeps dying; systemd restarts it, but it is not doing its job
    if active == "activating" and unit_state.get("sub") == "auto-restart":
        return "failed"   # died, waiting for Restart= to bring it back
    if active in ("active", "reloading", "activating"):
        return "running"
    if pids:
        return "running"  # e.g. started from the AIO Nav desktop app instead of systemd
    if active == "failed":
        return "failed"
    if unit_state.get("load") == "not-found":
        return "not_installed"
    if active in ("inactive", "deactivating"):
        return "stopped"
    return "unknown"


class ServicesCollector(PeriodicCollector):
    name = "services"
    section = "services"

    def __init__(self, store, cfg, fake=None, clock=time.monotonic):
        super().__init__(store, 2.0)
        self.services = cfg["services"]
        self.fake = fake
        self._clock = clock
        self._restarts: Dict[str, int] = {}          # key -> NRestarts seen last
        self._restarted_at: Dict[str, float] = {}    # key -> when the count last rose (or auto-restart was seen)
        self._reason: Dict[str, tuple] = {}          # key -> (time, last log line) cache

    def _crash_loop(self, key: str, unit_state: Dict) -> bool:
        """True while the unit restarted within CRASH_LOOP_WINDOW_S. The count is read from systemd
        (NRestarts); a sample taken during the restart wait (auto-restart) counts as well, so a unit
        that was already looping when the dashboard started is caught on the first such sample."""
        now = self._clock()
        try:
            count = int(unit_state.get("restarts") or 0)
        except ValueError:
            count = 0
        prev = self._restarts.get(key)
        self._restarts[key] = count
        if (prev is not None and count > prev) or unit_state.get("sub") == "auto-restart":
            self._restarted_at[key] = now
        at = self._restarted_at.get(key)
        return at is not None and now - at < CRASH_LOOP_WINDOW_S

    def _unit_reason(self, key: str, unit: str, user: bool) -> str:
        now = self._clock()
        cached = self._reason.get(key)
        if cached and now - cached[0] < 10.0:
            return cached[1]
        reason = unit_last_message(unit, user=user)
        self._reason[key] = (now, reason)
        return reason

    def poll(self) -> Dict:
        out = {}
        for key, svc in self.services.items():
            if self.fake is not None:
                state = self.fake.service_state(key)
                pid = self.fake.get().get("pids", {}).get(key, 1000 + len(key))
                out[key] = {"label": svc.get("label", key), "unit": svc.get("unit", ""),
                            "state": state, "pids": [pid] if state == "running" else [],
                            "unit_state": {"active": "active" if state == "running" else "inactive"},
                            "restartable": bool(svc.get("restartable"))}
                continue
            if svc.get("user_unit"):
                unit_state = systemd_unit_state(svc["user_unit"], user=True)
            else:
                unit_state = systemd_unit_state(svc["unit"]) if svc.get("unit") else {"load": "none"}
            pids = pgrep(svc["process_pattern"]) if svc.get("process_pattern") else None
            unit_name = svc.get("user_unit") or svc.get("unit", "")
            if unit_name and unit_state.get("load") not in ("not-found", "unknown", None):
                unit_state["crash_loop"] = self._crash_loop(key, unit_state)
                if unit_state["crash_loop"] or unit_state.get("active") == "failed":
                    unit_state["reason"] = self._unit_reason(key, unit_name, bool(svc.get("user_unit")))
            state = summarize(unit_state, pids)
            if state in ("not_installed", "unknown") and svc.get("process_pattern"):
                state = "stopped"  # detected by process too (e.g. desktop app): no unit is fine
            out[key] = {
                "label": svc.get("label", key),
                "unit": svc.get("user_unit") or svc.get("unit", ""),
                "user_unit": bool(svc.get("user_unit")),
                "state": state,
                "pids": pids or ([unit_state["main_pid"]] if unit_state.get("main_pid") else []),
                "unit_state": unit_state,
                "restartable": bool(svc.get("restartable")),
                # More than one process for a single program (e.g. started twice by two managers).
                "duplicate": len(pids or []) > 1,
            }
        return out
