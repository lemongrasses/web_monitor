"""Whitelisted maintenance actions.

Actions are identified by a fixed id and a target that must be in that
action's whitelist (derived from config). No command strings are ever
accepted from the client. Each run has a timeout, returns success/failure and
is recorded in the event log.
"""

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List

from . import diagnostics, process_control, service_control

logger = logging.getLogger(__name__)


@dataclass
class Action:
    id: str
    label: str
    targets: Dict[str, str]  # target key -> display label
    confirm: bool
    timeout_s: float
    run: Callable[[str], Dict]
    noun: str  # used in event titles, e.g. "Camera diagnostic PASS"


class ActionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ActionRegistry:
    def __init__(self, cfg, store, events, network_probe, fake=None):
        self.cfg = cfg
        self.events = events
        self._busy: set = set()
        self._lock = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="action")
        acfg = cfg["actions"]
        devices = {k: d.get("label", k) for k, d in cfg["devices"].items()}
        restartable = {k: s.get("label", k) for k, s in cfg["services"].items()
                       if s.get("restartable") and s.get("unit")}
        launchable = process_control.targets(cfg)
        grace = acfg["stop_grace_s"]
        self.actions: Dict[str, Action] = {
            "run_diagnostic": Action(
                "run_diagnostic", "Run diagnostic", devices, False, acfg["diagnostic_timeout_s"],
                lambda target: diagnostics.run_device_diagnostic(cfg, store, network_probe, target),
                "diagnostic"),
            "restart_service": Action(
                "restart_service", "Restart driver", restartable, True, acfg["restart_timeout_s"],
                lambda target: service_control.restart_service(cfg, target, acfg["restart_timeout_s"],
                                                               fake),
                "restart"),
            "start_process": Action(
                "start_process", "Start", launchable, False, acfg["restart_timeout_s"],
                lambda target: process_control.control(cfg, target, "start", grace, fake),
                "start"),
            "stop_process": Action(
                "stop_process", "Stop", launchable, True, acfg["restart_timeout_s"] + grace,
                lambda target: process_control.control(cfg, target, "stop", grace, fake),
                "stop"),
            "restart_process": Action(
                "restart_process", "Restart", launchable, True, acfg["restart_timeout_s"] + grace,
                lambda target: process_control.control(cfg, target, "restart", grace, fake),
                "restart"),
        }

    def describe(self) -> List[Dict]:
        return [{"id": a.id, "label": a.label, "targets": a.targets, "confirm": a.confirm,
                 "timeout_s": a.timeout_s} for a in self.actions.values()]

    def run(self, action_id: str, target: str) -> Dict:
        action = self.actions.get(action_id)
        if action is None:
            raise ActionError(f"unknown action {action_id!r}", 404)
        if target not in action.targets:
            raise ActionError(f"target {target!r} not allowed for {action_id}", 400)
        key = (action_id, target)
        with self._lock:
            if key in self._busy:
                raise ActionError("action already running", 409)
            self._busy.add(key)
        title = f"{action.targets[target]} {action.noun}"
        started = time.time()
        try:
            future = self._pool.submit(action.run, target)
            try:
                result = future.result(timeout=action.timeout_s)
            except concurrent.futures.TimeoutError:
                result = {"success": False, "summary": f"timed out after {action.timeout_s:.0f} s"}
            except Exception as e:  # report, never crash the request
                logger.exception("action %s failed", action_id)
                result = {"success": False, "summary": f"error: {e}"}
        finally:
            with self._lock:
                self._busy.discard(key)
        result.update({"action": action_id, "target": target, "duration_s": time.time() - started})
        is_diag = action_id == "run_diagnostic"
        outcome = ("PASS" if result["success"] else "FAIL") if is_diag else \
                  ("SUCCESS" if result["success"] else "FAILED")
        self.events.add("success" if result["success"] else "fault",
                        "diagnostic" if is_diag else "action",
                        f"{title} {outcome}", result.get("summary", ""),
                        action=action_id, target=target)
        result["outcome"] = outcome
        return result
