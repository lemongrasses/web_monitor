"""Start-up coordinator: run AIO NAV (and DSO, the nav.control_group) without an operator. It
starts them after power-on, restarts them after a failure once the conditions allow it, and
says at every moment what it is doing (readiness reasons, the 0x05 status, events).

Rules, in order:
* Off unless ``autostart.enabled`` and ``nav.allow_control`` (the dashboard owns AIO NAV).
* An operator Stop is a *hold*: nothing starts automatically until someone presses Start or the
  computer is switched on again (kept with the kernel boot id, so a dashboard restart keeps it).
* It never gives up (``max_attempts: 0``): after a failure it waits ``retry_backoff_s`` (the last
  value repeats) and tries again. The count is kept for this power-on, so a dashboard that keeps
  restarting does not shorten the waits; it starts again from the first wait once READY is reached.
* Before a start, the ``prerequisites`` (readiness conditions such as imu) must hold for
  ``prerequisite_stable_s``, and (``require_still``) the IMU must show the vehicle still for
  ``still_stable_s``: AIO NAV's first alignment step needs it, and a start may follow a crash or a
  watchdog reboot while driving. After READY the vehicle is told SAFE_STOP meanwhile.
* AIO NAV running but without output: if a prerequisite is missing (no IMU data, no valid time) the
  cause is upstream, so it is left running and may resume by itself; with the inputs fine it gets
  ``stale_grace_s`` to recover, then it is stopped and started again.
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
from .registry import ActionError
from ..state.readiness import Condition

logger = logging.getLogger(__name__)

BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
# After its own stop, the services / readiness snapshots still show the old run for a poll or two
# (services every 2 s): judging that run again would schedule the retry and stop it again.
STOP_SETTLE_S = 10.0
# Where the state goes when state/ cannot be written (disk full, file system read-only): in memory
# (tmpfs), so an operator Stop still holds across a dashboard restart; gone at power-off.
FALLBACK_FILE = Path("/dev/shm/aio-dashboard-autostart.json")
BUSY_RETRY_S = 2.0         # another start/stop holds the process slot: try again this soon


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
        self.still = Condition(float(a["still_stable_s"]), 0.0)
        self.require_still = bool(a["require_still"])
        self.stale_grace_s = float(a["stale_grace_s"])
        self.backoff = [float(x) for x in a["retry_backoff_s"]] or [5.0]
        self.max_attempts = int(a["max_attempts"] or 0)         # 0: never give up
        self._file = cfg.state_file("autostart.json")
        self._fallback = FALLBACK_FILE
        self._save_failed = False
        saved, spare = self._load(self._file), self._load(self._fallback)
        boot = self._boot_id

        def this_boot(key):            # a record from either place, made in this power-on
            return bool(boot) and boot in (saved.get(key), spare.get(key))
        # An operator Stop or READY recorded in either place counts (fail towards holding). Both
        # end with the power-on: switching the computer on again is a fully automatic start.
        self.hold = bool(saved.get("hold") or spare.get("hold")) and this_boot("hold_boot_id")
        self.ready_this_boot = this_boot("ready_boot_id")
        self.state, self.detail = ("disabled", "") if not self.enabled else ("waiting", "")
        self.attempts = int((saved if saved.get("boot_id") == boot else spare).get("attempts") or 0) \
            if this_boot("boot_id") else 0
        self.next_at: Optional[float] = None
        if self.attempts:                      # restarted while retrying: keep the wait
            self.next_at = self._clock() + self._wait_s()
        self._failed_since: Optional[float] = None
        self.last_tick: Optional[float] = None       # monotonic time of the last tick (watchdog)
        self._launching = False
        self._was_running = False
        self._stopped_at: Optional[float] = None    # end of its own stop, until the snapshots show it
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- persistence
    @staticmethod
    def _load(path: Path) -> Dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(path: Path, data: Dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _save(self) -> None:
        data = {"hold": self.hold, "boot_id": self._boot_id, "attempts": self.attempts}
        if self.hold:
            data["hold_boot_id"] = self._boot_id
        if self.ready_this_boot:
            data["ready_boot_id"] = self._boot_id
        try:
            self._write(self._file, data)
        except OSError as e:
            logger.warning("cannot save the start-up state: %s", e)
            try:
                self._write(self._fallback, data)
                where = "kept in memory until the next power-off"
            except OSError as e2:
                logger.warning("cannot save the start-up state in memory either: %s", e2)
                where = "kept only until the dashboard restarts"
            if not self._save_failed:
                self._save_failed = True
                self.events.add("warning", "autostart", "Cannot save the start-up state",
                                f"{e}; an operator Stop is {where}")
            return
        self._save_failed = False
        try:
            self._fallback.unlink()                 # saved properly again: the spare copy is stale
        except OSError:
            pass

    # ----------------------------------------------------------- operator input
    def on_action(self, action_id: str, target: str) -> None:
        """Called after every maintenance / Overview action. An operator Stop of AIO NAV holds the
        automatic start; an operator Start or Restart releases it and starts the waits afresh."""
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
                changed = self.hold or self.attempts
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
        self.last_tick = time.monotonic()
        if not self.enabled:
            return
        now = self._clock()
        services = self.store.get("services", {}) or {}
        running = bool((services.get(self.cfg["nav"]["service"]) or {}).get("pids"))
        r = self.store.get("readiness", {}) or {}
        with self._lock:
            if r.get("state") == "READY" and (not self.ready_this_boot or self.attempts):
                if not self.ready_this_boot:
                    self.ready_this_boot = True    # a later stop asks the vehicle for a safe stop
                    self.events.add("info", "autostart", "Ready reached",
                                    "after a failure AIO NAV is restarted once the vehicle is still")
                self.attempts, self.next_at = 0, None          # a good run: the waits start afresh
                self._save()
            if self._launching:
                return
            if self.hold:
                self._set("held", "Stopped by an operator; automatic start resumes after Start "
                                  "or the next power-on")
                return
            if not running:
                self._stopped_at = None
            elif self._stopped_at is not None and now - self._stopped_at < STOP_SETTLE_S:
                return                             # its own stop is not in the snapshots yet
            if running:
                self._stopped_at = None
                self._was_running = True
                self._check_running(r, now)
                return
            self._failed_since = None
            if self._was_running:                 # it stopped by itself (crash, killed)
                self._was_running = False
                if self.attempts:
                    self._retry_later(now, "AIO NAV stopped")
            if self.max_attempts and self.attempts >= self.max_attempts:
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
            self._save()
            self._launch("start", now)

    def _check_running(self, r: Dict, now: float) -> None:
        """AIO NAV runs: restart it only when its own output stopped and its inputs are fine."""
        inhibit = set(r.get("inhibit") or [])
        failed = r.get("state") in ("FAULT", "DEGRADED") and inhibit & {"STARTUP_TIMEOUT", "NAV_OUTPUT_STALE"}
        if not failed:
            self._failed_since = None
            self._set("running", "")
            return
        why = "sends no navigation output" if "STARTUP_TIMEOUT" in inhibit else "navigation output stopped"
        missing = self._missing_prerequisites(r, now, still=False)
        if missing:                                # upstream: a restart would not help
            self._failed_since = None
            self._set("waiting_inputs", f"Waiting for {', '.join(missing)}; AIO NAV may resume by itself")
            return
        if self._failed_since is None:
            self._failed_since = now
        grace = 0.0 if "STARTUP_TIMEOUT" in inhibit else self.stale_grace_s
        left = grace - (now - self._failed_since)
        if left > 0:
            self._set("recovering", f"Restarting AIO NAV in {left:.0f} s if it does not resume")
            return
        if self.max_attempts and self.attempts >= self.max_attempts:   # left running for the logs
            self._set("gave_up", f"AIO NAV started {self.attempts} times; still {why}")
            return
        self._failed_since = None
        self._retry_later(now, f"AIO NAV {why}; restarting it")
        self._launch("stop", now)

    def _missing_prerequisites(self, r: Dict, now: float, still: bool = True):
        conds = {c["name"]: c for c in (r.get("conditions") or [])}
        missing = []
        for name, cond in self.prereq.items():
            c = conds.get(name) or {}
            if not cond.update(c.get("ok"), now):
                label = c.get("label") or name
                why = c.get("reason") if c.get("ok") is False else ""   # e.g. an IMU without valid time
                missing.append(f"{label}: {why}" if why else label)
        if still and self.require_still:
            m = r.get("motion") or {}
            if not self.still.update(m.get("still"), now):
                missing.append("the vehicle to be still" + (f" ({m['reason']})" if m.get("reason") else ""))
        return missing

    def _wait_s(self) -> float:
        return self.backoff[min(max(self.attempts - 1, 0), len(self.backoff) - 1)]

    def _retry_later(self, now: float, why: str) -> None:
        wait = self._wait_s()
        self.next_at = now + wait
        self._set("retry_wait", f"{why}; next try in {wait:.0f} s")
        self.events.add("warning", "autostart", "Automatic start: retry scheduled", f"{why}; in {wait:.0f} s")

    def _launch(self, verb: str, now: float) -> None:
        """Start or stop the control group on a worker thread (it takes seconds)."""
        self._launching = True
        if verb == "start":
            of = f" of {self.max_attempts}" if self.max_attempts else ""
            self._set("starting", f"Starting AIO NAV (attempt {self.attempts}{of})")

        def work():
            try:
                if verb == "start" and self.on_start:
                    self.on_start()
                action = "start_process" if verb == "start" else "stop_process"
                res = self.actions.run(action, process_control.GROUP)
                if verb == "start" and not res.get("success"):
                    with self._lock:
                        self._retry_later(self._clock(), res.get("summary") or "AIO NAV did not start")
            except ActionError as e:
                with self._lock:
                    if e.status == 409:                # busy (e.g. the DSO watchdog): not a failure
                        if verb == "start":
                            self.attempts = max(0, self.attempts - 1)
                        self.next_at = self._clock() + BUSY_RETRY_S
                        self._set("retry_wait", f"Waiting for another start or stop to finish ({e})")
                    else:
                        self._retry_later(self._clock(), f"automatic {verb} refused: {e}")
            except Exception as e:                     # never leave the coordinator stuck
                logger.exception("automatic %s failed", verb)
                with self._lock:
                    self._retry_later(self._clock(), f"automatic {verb} failed: {e}")
            finally:
                with self._lock:
                    self._launching = False
                    if verb == "stop":
                        self._was_running = False
                        self._stopped_at = self._clock()

        threading.Thread(target=work, name=f"autostart-{verb}", daemon=True).start()
