"""Start / stop / restart of whitelisted systemd units.

The unit name always comes from config (``services.<key>.unit``), never from
the request, and the verb is one of a fixed set. The dashboard runs
unprivileged; deploy/install.sh writes a sudoers rule that grants exactly
``systemctl restart <unit>`` for ``restartable`` units and ``start|stop|restart``
for ``controllable`` units.
"""

import subprocess
import time
from typing import Dict

from ..collectors.services import systemd_unit_state

VERBS = {"start": "started", "stop": "stopped", "restart": "restarted"}


def control_service(cfg, key: str, verb: str, timeout_s: float, fake=None) -> Dict:
    if verb not in VERBS:
        return {"success": False, "summary": f"unsupported action {verb!r}"}
    svc = cfg["services"][key]
    unit = svc["unit"]
    if cfg.fake:
        time.sleep(1.5)
        if fake is not None and verb in ("start", "stop"):
            fake.set_service(key, "running" if verb == "start" else "stopped")
        return {"success": True, "summary": f"(fake) {VERBS[verb]} {unit}"}

    try:
        res = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", verb, unit],
                             capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"success": False, "summary": f"systemctl {verb} {unit} timed out"}
    except OSError as e:
        return {"success": False, "summary": str(e)}
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        return {"success": False, "summary": msg[-1] if msg else f"exit code {res.returncode}"}

    state = systemd_unit_state(unit)
    if verb == "stop":
        ok = state.get("active") in ("inactive", "failed")
    else:
        ok = state.get("active") in ("active", "activating")
    return {"success": ok, "summary": f"{unit} is {state.get('active')} ({state.get('sub', '')})",
            "unit_state": state}


def restart_service(cfg, key: str, timeout_s: float, fake=None) -> Dict:
    return control_service(cfg, key, "restart", timeout_s, fake)
