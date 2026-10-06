"""systemd unit status for configured services (plus process fallback)."""

import subprocess
from typing import Dict, List, Optional

from .base import PeriodicCollector


def systemd_unit_state(unit: str, timeout: float = 3.0) -> Dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "--no-pager",
             "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
             "-p", "MainPID", "-p", "ActiveEnterTimestamp", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=timeout, check=False,
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
    }


def pgrep(pattern: str, timeout: float = 2.0) -> List[int]:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True,
                             timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(x) for x in out.stdout.split() if x.isdigit()]


def summarize(unit_state: Dict, pids: Optional[List[int]]) -> str:
    """running | stopped | failed | not_installed | unknown."""
    active = unit_state.get("active")
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

    def __init__(self, store, cfg, fake=None):
        super().__init__(store, 2.0)
        self.services = cfg["services"]
        self.fake = fake

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
            unit_state = systemd_unit_state(svc["unit"]) if svc.get("unit") else {"load": "none"}
            pids = pgrep(svc["process_pattern"]) if svc.get("process_pattern") else None
            state = summarize(unit_state, pids)
            if state in ("not_installed", "unknown") and svc.get("process_pattern"):
                state = "stopped"  # detected by process too (e.g. desktop app): no unit is fine
            out[key] = {
                "label": svc.get("label", key),
                "unit": svc.get("unit", ""),
                "state": state,
                "pids": pids or ([unit_state["main_pid"]] if unit_state.get("main_pid") else []),
                "unit_state": unit_state,
                "restartable": bool(svc.get("restartable")),
            }
        return out
