"""DSO watchdog: restart DSO when its odometry output turns NaN.

DSO (``dso_live``) publishes ``/dso/odometry``. When tracking breaks it publishes NaN
values and has to be restarted. This watches that topic and restarts only the DSO
process (the same restart the maintenance API offers); AIO NAV keeps running.

Safeguards against restart loops: DSO is left alone for ``settle_s`` after a restart,
restarts are at least ``cooldown_s`` apart, and after ``max_restarts`` within
``window_s`` the watchdog stops restarting and says so (state ``gave_up``) until the
odometry is finite again. It acts only while the DSO process is running, so it never
starts DSO that was stopped on purpose.

The state machine (``DsoWatchdog``) takes the clock and callbacks as arguments so it can
be tested without ROS; ``attach`` connects it to the ROS monitor's node.
"""

import logging
import math
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

logger = logging.getLogger(__name__)

OK = "ok"
BAD = "bad"
SETTLING = "settling"
RESTARTING = "restarting"
GAVE_UP = "gave_up"
IDLE = "idle"            # DSO not running
WAITING = "waiting"      # running, no odometry yet
DISABLED = "disabled"


def odometry_finite(msg) -> bool:
    """False if any pose or twist number in a nav_msgs/Odometry is NaN or infinite."""
    p, q = msg.pose.pose.position, msg.pose.pose.orientation
    lin, ang = msg.twist.twist.linear, msg.twist.twist.angular
    return all(math.isfinite(v) for v in (p.x, p.y, p.z, q.x, q.y, q.z, q.w,
                                          lin.x, lin.y, lin.z, ang.x, ang.y, ang.z))


class DsoWatchdog:
    section = "watchdog"

    def __init__(self, cfg, store, events, restart: Callable[[], Dict],
                 process_running: Callable[[], bool], clock: Callable[[], float] = time.monotonic):
        self.w = cfg["dso_watchdog"]
        self.store, self.events = store, events
        self._restart, self._running, self._clock = restart, process_running, clock
        self._lock = threading.Lock()
        self._bad = 0
        self._last_msg: Optional[float] = None
        self._last_ok: Optional[float] = None
        self._settle_until = 0.0
        self._last_restart: Optional[float] = None
        self._history: Deque[float] = deque()
        self._busy = False
        self._gave_up_logged = False
        self.state = DISABLED if not self.w["enabled"] else WAITING
        self.detail = ""
        self.restarts_total = 0
        self.last_restart_ts: Optional[float] = None   # wall clock, for display
        self.last_result = ""

    @property
    def enabled(self) -> bool:
        return bool(self.w["enabled"])

    # ----------------------------------------------------------------- input
    def on_message(self, finite: bool) -> None:
        now = self._clock()
        with self._lock:
            self._last_msg = now
            if finite:
                self._bad = 0
                self._last_ok = now
                self._gave_up_logged = False
            else:
                self._bad += 1

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        """Evaluate once (called about once a second); may start a restart."""
        if not self.enabled:
            self._publish()
            return
        now = self._clock()
        start_restart = False
        with self._lock:
            if self._busy:
                self.state, self.detail = RESTARTING, "restarting DSO"
            elif not self._running():
                self._bad = 0
                self.state, self.detail = IDLE, "DSO is not running"
            elif now < self._settle_until:
                self._bad = 0
                self.state, self.detail = SETTLING, f"DSO started, waiting {self._settle_until - now:.0f} s"
            elif self._bad >= int(self.w["bad_messages"]):
                while self._history and now - self._history[0] > float(self.w["window_s"]):
                    self._history.popleft()
                if len(self._history) >= int(self.w["max_restarts"]):
                    self.state = GAVE_UP
                    self.detail = (f"{len(self._history)} restarts in "
                                   f"{float(self.w['window_s']) / 60:.0f} min, not restarting again")
                    if not self._gave_up_logged:
                        self._gave_up_logged = True
                        self.events.add("fault", "watchdog", "DSO watchdog gave up", self.detail)
                elif self._last_restart is not None and now - self._last_restart < float(self.w["cooldown_s"]):
                    self.state, self.detail = BAD, "odometry is NaN, waiting before the next restart"
                else:
                    self._busy = True
                    start_restart = True
                    self.state, self.detail = RESTARTING, "odometry is NaN, restarting DSO"
            elif self._last_msg is None:
                self.state, self.detail = WAITING, "no odometry received yet"
            else:
                self.state, self.detail = OK, "odometry is finite"
        if start_restart:
            threading.Thread(target=self._do_restart, name="dso-watchdog", daemon=True).start()
        self._publish()

    def _do_restart(self) -> None:
        self.events.add("warning", "watchdog", "DSO odometry is NaN: restarting DSO",
                        "AIO NAV is not restarted")
        try:
            result = self._restart()
        except Exception as e:  # keep the watchdog alive whatever the action does
            logger.exception("DSO restart failed")
            result = {"success": False, "summary": f"error: {e}"}
        now = self._clock()
        with self._lock:
            self._busy = False
            self._bad = 0
            self._last_restart = now
            self._history.append(now)
            self._settle_until = now + float(self.w["settle_s"])
            self.restarts_total += 1
            self.last_restart_ts = time.time()
            self.last_result = ("restarted" if result.get("success") else "failed") + \
                (": " + result["summary"] if result.get("summary") else "")
        self._publish()

    # ---------------------------------------------------------------- output
    def status(self) -> Dict:
        with self._lock:
            return {
                "enabled": self.enabled, "state": self.state, "detail": self.detail,
                "topic": self.w["topic"], "restarts": self.restarts_total,
                "last_restart_ts": self.last_restart_ts, "last_result": self.last_result,
                "bad_messages": self._bad,
                "last_msg_age_s": None if self._last_msg is None else self._clock() - self._last_msg,
            }

    def _publish(self) -> None:
        self.store.set(self.section, {"dso": self.status()})

    # ------------------------------------------------------------------- ROS
    def attach(self, node, qos) -> None:
        """Subscribe to the odometry topic on the ROS monitor's node and start ticking."""
        from nav_msgs.msg import Odometry
        node.create_subscription(Odometry, self.w["topic"],
                                 lambda msg: self.on_message(odometry_finite(msg)), qos)
        node.create_timer(1.0, self.tick)
        logger.info("DSO watchdog on %s", self.w["topic"])
