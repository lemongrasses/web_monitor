"""Start-up coordinator: start AIO NAV (and DSO, the nav.control_group) after power-on, so the
operator only has to switch the system on and follow the prompts.

Rules, in order:
* Off unless ``autostart.enabled`` and ``nav.allow_control`` (the dashboard owns AIO NAV).
* An operator Stop is a *hold*: nothing starts automatically until someone presses Start. The
  hold is kept on disk, so it survives a reboot.
* Once READY was reached in this boot, AIO NAV is never started or restarted automatically
  again: the vehicle may be moving. This is kept on disk with the kernel boot id, so a dashboard
  restart (systemd Restart=always) cannot undo it, while the next power-on starts afresh.
* Before READY: wait until the configured prerequisites (readiness conditions such as imu, gnss)
  have held for ``prerequisite_stable_s``, start, and if AIO NAV dies or sends nothing in time,
  stop it and try again after ``retry_backoff_s``, at most ``max_attempts`` times.
* A program already running (dashboard restart, desktop app) is adopted, never started twice:
  process_control.start_one checks for it under a lock.

It reads only the snapshots other parts publish (services, readiness) and runs the start on its
own thread: a start takes several seconds.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from . import process_control
from ..state.readiness import Condition

logger = logging.getLogger(__name__)

BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"


def read_boot_id() -> str:
    try:
        return Path(BOOT_ID_FILE).read_text().strip()
    except OSError:
        return ""


class StartupCoordinator:
    def __init__(self, cfg, store, actions, events, on_start: Optional[Callable[[], None]] = None,
                 clock=time.monotonic, boot_id: Callable[[], str] = read_boot_id, interval_s: float = 1.0):
        self.cfg, self.store, self.actions, self.events = cfg, store, actions, events
        self.on_start = on_start
        self._clock, self._boot_id, self.interval_s = clock, boot_id(), interval_s
        a = cfg["autostart"]
        self.enabled = bool(a["enabled"] and cfg["nav"]["allow_control"])
        self.prereq = {n: Condition(float(a["prerequisite_stable_s"]), 0.0) for n in a["prerequisites"]}
        self.backoff = [float(x) for x in a["retry_backoff_s"]] or [5.0]
        self.max_attempts = int(a["max_attempts"])
        self._file = cfg.state_file("autostart.json")
        saved = self._load()
        self.hold = bool(saved.get("hold"))
        self.ready_this_boot = bool(self._boot_id) and saved.get("ready_boot_id") == self._boot_id
        self.state, self.detail = ("disabled", "") if not self.enabled else ("waiting", "")
        self.attempts = 0
        self.next_at: Optional[float] = None
        self._launching = False
        self._was_running = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- persistence
    def _load(self) -> Dict:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            data = {"hold": self.hold}
            if self.ready_this_boot:
                data["ready_boot_id"] = self._boot_id
            tmp = self._file.with_name(self._file.name + ".tmp")
            tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
            tmp.replace(self._file)
        except OSError as e:
            logger.warning("cannot save the start-up state: %s", e)

    # ----------------------------------------------------------- operator input
    def on_action(self, action_id: str, target: str) -> None:
        """Called after every maintenance / Overview action. An operator Stop of AIO NAV holds the
        automatic start; an operator Start or Restart releases it and allows new attempts."""
        nav = (process_control.GROUP, self.cfg["nav"]["service"])
        if target not in nav or self._launching:
            return
        with self._lock:
            if action_id == "stop_process":
                if not self.hold:
                    self.hold = True
                    self._save()
                    self.events.add("info", "autostart", "Automatic start on hold", "AIO NAV stopped by an operator")
            elif action_id in ("start_process", "restart_process"):
                changed = self.hold
                self.hold, self.attempts, self.next_at = False, 0, None
                if self.state == "gave_up":
                    self.state = "waiting"
                if changed:
                    self._save()

    # ----------------------------------------------------------------- status
    def status(self) -> Dict:
        with self._lock:
            wait = None if self.next_at is None else max(0.0, self.next_at - self._clock())
            return {"enabled": self.enabled, "state": self.state, "detail": self.detail,
                    "hold": self.hold if self.enabled else False, "attempts": self.attempts,
                    "max_attempts": self.max_attempts, "retry_in_s": None if wait is None else round(wait, 1),
                    "ready_this_boot": self.ready_this_boot}

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, name="autostart", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("start-up coordinator failed")
            self._stop.wait(self.interval_s)

    def _set(self, state: str, detail: str = "") -> None:
        if state != self.state:
            logger.info("autostart: %s %s", state, detail)
        self.state, self.detail = state, detail

    def tick(self) -> None:
        if not self.enabled:
            return
        now = self._clock()
        services = self.store.get("services", {}) or {}
        running = bool((services.get(self.cfg["nav"]["service"]) or {}).get("pids"))
        r = self.store.get("readiness", {}) or {}
        with self._lock:
            if r.get("state") == "READY" and not self.ready_this_boot:
                self.ready_this_boot = True        # from now on: never an automatic (re)start this boot
                self._save()
                self.events.add("info", "autostart", "Ready reached; no automatic restart until the next power-on")
            if self._launching:
                return
            if self.hold:
                self._set("held", "Stopped by an operator; press Start to resume")
                return
            if self.ready_this_boot:
                self._set("done" if running else "ready_this_boot",
                          "" if running else "AIO NAV stopped after it was ready; it is not restarted automatically")
                self._was_running = running
                return
            if running:
                self._was_running = True
                inhibit = set(r.get("inhibit") or [])
                failed = r.get("state") == "FAULT" and inhibit & {"STARTUP_TIMEOUT", "NAV_OUTPUT_STALE"}
                if failed:
                    why = "no navigation output" if "STARTUP_TIMEOUT" in inhibit else "navigation output stopped"
                    if self.attempts < self.max_attempts:
                        self._retry_later(now, f"AIO NAV started but {why}")
                        self._launch("stop", now)
                    else:                          # left running so the logs can be read
                        self._set("gave_up", f"AIO NAV started {self.attempts} times; still {why}")
                    return
                self._set("running", "")
                return
            if self._was_running:                 # it was running and stopped by itself before READY
                self._was_running = False
                if self.attempts:
                    self._retry_later(now, "AIO NAV stopped before it was ready")
            if self.attempts >= self.max_attempts:
                self._set("gave_up", f"AIO NAV did not become ready after {self.attempts} attempts")
                return
            if self.next_at is not None and now < self.next_at:
                self._set("retry_wait", self.detail or "Waiting to try again")
                return
            missing = self._missing_prerequisites(r, now)
            if missing:
                self._set("waiting", "Waiting for " + ", ".join(missing))
                return
            self.attempts += 1
            self._launch("start", now)

    def _missing_prerequisites(self, r: Dict, now: float):
        conds = {c["name"]: c for c in (r.get("conditions") or [])}
        missing = []
        for name, cond in self.prereq.items():
            c = conds.get(name) or {}
            if not cond.update(c.get("ok"), now):
                missing.append(c.get("label") or name)
        return missing

    def _retry_later(self, now: float, why: str) -> None:
        wait = self.backoff[min(max(self.attempts - 1, 0), len(self.backoff) - 1)]
        self.next_at = now + wait
        self._set("retry_wait", f"{why}; next try in {wait:.0f} s")
        self.events.add("warning", "autostart", "Automatic start: retry scheduled", f"{why}; in {wait:.0f} s")

    def _launch(self, verb: str, now: float) -> None:
        """Start or stop the control group on a worker thread (it takes seconds)."""
        self._launching = True
        if verb == "start":
            self._set("starting", f"Starting AIO NAV (attempt {self.attempts} of {self.max_attempts})")

        def work():
            try:
                if verb == "start" and self.on_start:
                    self.on_start()
                action = "start_process" if verb == "start" else "stop_process"
                res = self.actions.run(action, process_control.GROUP)
                if verb == "start" and not res.get("success"):
                    with self._lock:
                        self._retry_later(self._clock(), res.get("summary") or "AIO NAV did not start")
            except Exception as e:                     # never leave the coordinator stuck
                logger.exception("automatic %s failed", verb)
                with self._lock:
                    self._retry_later(self._clock(), f"automatic {verb} failed: {e}")
            finally:
                with self._lock:
                    self._launching = False
                    if verb == "stop":
                        self._was_running = False

        threading.Thread(target=work, name=f"autostart-{verb}", daemon=True).start()
