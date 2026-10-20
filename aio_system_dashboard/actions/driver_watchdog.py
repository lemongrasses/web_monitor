"""Sensor driver watchdog: restart the driver stack when it runs but its IMU sends nothing.

systemd restarts the driver unit only when its process exits with an error (Restart=on-failure).
A driver that still runs but no longer publishes (a USB or network hiccup, a stuck node) was left
alone, and the automatic start then waited for IMU data for ever. Rules:

* Live mode only: in Bag replay the IMU comes from the bag (another ROS domain), so a missing IMU
  says nothing about the drivers.
* The driver unit must be active, and the IMU topic missing or stale for ``stale_s`` without a
  break, or the unit failed (systemd gave up on it) for that long. An IMU that publishes without a
  valid time (no GNSS time yet) is not a driver fault.
* Restart the unit (``systemctl --user restart``), then wait ``retry_backoff_s`` before the next
  restart (the last value repeats); a healthy IMU starts the waits afresh.
* An operator Stop of the drivers (Maintenance) is respected until they are started again.
"""

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

BAD_STATES = ("missing", "stale")


class DriverWatchdog:
    def __init__(self, cfg, store, actions, events, clock=time.monotonic, interval_s: float = 2.0):
        self.cfg, self.store, self.actions, self.events = cfg, store, actions, events
        self._clock, self.interval_s = clock, interval_s
        w = cfg["driver_watchdog"]
        key = w.get("service") or "auto"
        if key == "auto":
            key = next((k for k, s in cfg["services"].items() if s.get("user_unit")), None)
        self.service: Optional[str] = key if key in cfg["services"] else None
        self.enabled = bool(w["enabled"] and self.service and not cfg.fake)
        self.stale_s = float(w["stale_s"])
        self.backoff = [float(x) for x in w["retry_backoff_s"]] or [60.0]
        self.restarts = 0                       # in a row, without a healthy IMU in between
        self.restarts_total = 0
        self.held = False                       # drivers stopped by an operator
        self._bad_since: Optional[float] = None
        self._last_restart: Optional[float] = None
        self._busy = False
        self.state, self.detail = ("disabled", "") if not self.enabled else ("ok", "")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------- input
    def on_action(self, action_id: str, target: str) -> None:
        if target != self.service:
            return
        with self._lock:
            if action_id == "stop_driver":
                self.held = True
            elif action_id in ("start_driver", "restart_driver") and not self._busy:
                self.held, self.restarts, self._bad_since = False, 0, None

    def status(self) -> Dict:
        with self._lock:
            return {"enabled": self.enabled, "state": self.state, "detail": self.detail,
                    "service": self.service, "restarts": self.restarts_total}

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, name="driver-watchdog", daemon=True)
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
                logger.exception("driver watchdog failed")
            self._stop.wait(self.interval_s)

    def _imu(self) -> Optional[Dict]:
        ros = self.store.get("ros", {}) or {}
        if not ros.get("available"):
            return None
        name = self.cfg["readiness"].get("imu_topic") or "auto"
        watched = ros.get("watched") or []
        if name == "auto":
            return next((t for t in watched if t.get("group") == "nav"), None)
        return next((t for t in watched if t.get("name") == name), None)

    def _set(self, state: str, detail: str = "") -> None:
        if state != self.state:
            logger.info("driver watchdog: %s %s", state, detail)
        self.state, self.detail = state, detail

    def tick(self) -> None:
        if not self.enabled:
            return
        now = self._clock()
        mode = self.cfg.ros_mode()
        svc = (self.store.get("services", {}) or {}).get(self.service) or {}
        unit = (svc.get("unit_state") or {}).get("active")
        active, failed = unit == "active", unit == "failed"   # failed: systemd gave up on it
        imu = self._imu()
        with self._lock:
            if self._busy:
                return
            if mode not in (None, "live"):
                self._bad_since = None
                self._set("idle", "Bag replay: the IMU comes from the bag")
                return
            if self.held:
                self._set("held", "Drivers stopped by an operator")
                return
            if not failed and (imu is None or not active):
                self._bad_since = None          # no ROS view, or the unit is (re)starting / stopped
                self._set("idle", "" if active else "driver unit not active")
                return
            if not failed and imu.get("state") not in BAD_STATES:
                self._bad_since, self.restarts = None, 0
                self._set("ok", "")
                return
            if self._bad_since is None:
                self._bad_since = now
            bad_for = now - self._bad_since
            what = "driver unit failed" if failed else f"IMU {imu['state']}"
            if bad_for < self.stale_s:
                self._set("watching", f"{what} for {bad_for:.0f} s")
                return
            if self._last_restart is not None and self.restarts:
                wait = self.backoff[min(self.restarts - 1, len(self.backoff) - 1)]
                left = wait - (now - self._last_restart)
                if left > 0:
                    self._set("retry_wait", f"{what}; restarting the drivers again in {left:.0f} s")
                    return
            self._busy = True
            self.restarts += 1
            self.restarts_total += 1
            self._last_restart = now
            self._set("restarting", f"{what} for {bad_for:.0f} s")
        self.events.add("warning", "drivers", "Sensor drivers restarted",
                        f"{what} for {bad_for:.0f} s ({svc.get('label') or self.service}; "
                        f"restart {self.restarts} in a row)")
        threading.Thread(target=self._restart, name="driver-restart", daemon=True).start()

    def _restart(self) -> None:
        try:
            res = self.actions.run("restart_driver", self.service)
            ok, why = bool(res.get("success")), res.get("summary") or ""
        except Exception as e:                     # busy (409) or anything else: try again later
            ok, why = False, str(e)
        with self._lock:
            self._busy = False
            self._bad_since = None                 # the restarted drivers get stale_s again
            self._set("restarted" if ok else "restart_failed", why)
