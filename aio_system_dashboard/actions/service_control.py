"""Restart of whitelisted systemd units.

The unit name always comes from config (``services.<key>.unit``), never from
the request. The dashboard runs unprivileged; deploy/aio-dashboard.sudoers
grants exactly ``systemctl restart <unit>`` for the restartable units.
"""

import subprocess
import time
from typing import Dict

from ..collectors.services import systemd_unit_state, user_bus_env


def restart_service(cfg, key: str, timeout_s: float, fake=None) -> Dict:
    svc = cfg["services"][key]
    unit = svc["unit"]
    if cfg.fake:
        time.sleep(1.5)
        return {"success": True, "summary": f"(fake) restarted {unit}"}

    try:
        res = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", unit],
                             capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"success": False, "summary": f"systemctl restart {unit} timed out"}
    except OSError as e:
        return {"success": False, "summary": str(e)}
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        return {"success": False, "summary": msg[-1] if msg else f"exit code {res.returncode}"}

    state = systemd_unit_state(unit)
    ok = state.get("active") in ("active", "activating")
    return {"success": ok, "summary": f"{unit} is {state.get('active')} ({state.get('sub', '')})",
            "unit_state": state}


USER_VERBS = {"start": "started", "stop": "stopped", "restart": "restarted"}


def control_user_service(cfg, key: str, verb: str, timeout_s: float, fake=None) -> Dict:
    """Start / stop / restart a systemd *user* unit (``services.<key>.user_unit``), e.g. the
    sensor driver stack. It runs as the dashboard's user, so no sudo is involved."""
    if verb not in USER_VERBS:
        return {"success": False, "summary": f"unsupported action {verb!r}"}
    unit = cfg["services"][key]["user_unit"]
    if cfg.fake:
        time.sleep(1.5)
        if fake is not None:
            fake.set_service(key, "stopped" if verb == "stop" else "running")
        return {"success": True, "summary": f"(fake) {USER_VERBS[verb]} {unit}"}
    try:
        res = subprocess.run(["systemctl", "--user", verb, unit], capture_output=True, text=True,
                             timeout=timeout_s, check=False, env=user_bus_env())
    except subprocess.TimeoutExpired:
        return {"success": False, "summary": f"systemctl --user {verb} {unit} timed out"}
    except OSError as e:
        return {"success": False, "summary": str(e)}
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        return {"success": False, "summary": msg[-1] if msg else f"exit code {res.returncode}"}
    state = systemd_unit_state(unit, user=True)
    if verb == "stop":
        ok = state.get("active") in ("inactive", "failed")
    else:
        ok = state.get("active") in ("active", "activating")
    return {"success": ok, "summary": f"{unit} is {state.get('active')} ({state.get('sub', '')})",
            "unit_state": state}
