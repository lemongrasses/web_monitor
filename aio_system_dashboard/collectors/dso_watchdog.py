"""DSO watchdog: keep DSO healthy without touching AIO NAV.

Two situations, both handled by restarting or starting only DSO:

* Odometry turns NaN. When DSO loses tracking, ``/dso/odometry`` carries NaN. DSO is restarted
  at once (``bad_messages`` NaN messages in a row, 1 by default).
* DSO is not running although it should be (it crashed, or failed right after a start). It is
  started again every ``retry_s`` seconds (30 by default) until it stays up.

"Should be running" is recorded by the start / stop actions (``process_control.wanted``), so a DSO
that was stopped on purpose stays stopped, and one that is running counts as wanted.

If NaN keeps coming back (``fast_restarts`` restarts within ``window_s``), further restarts also
wait ``retry_s`` so a broken camera view does not make DSO restart in a tight loop.

The state machine takes the clock and callbacks as arguments so it can be tested without ROS;
``attach`` connects it to the ROS monitor's node.
"""

import logging
import math
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional

logger = logging.getLogger(__name__)

OK = "ok"
BAD = "bad"              # NaN seen, waiting for the minimum gap before restarting
SETTLING = "settling"    # just restarted
RESTARTING = "restarting"
RETRYING = "retrying"    # should be running, is not: waiting for the next try
IDLE = "idle"            # not running and not wanted
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

    def __init__(self, cfg, store, events, restart: Callable[[], Dict], start: Callable[[], Dict],
                 process_running: Callable[[], bool], wanted: Callable[[], bool],
                 adopt: Callable[[], None], clock: Callable[[], float] = time.monotonic):
        self.w = cfg["dso_watchdog"]
        self.store, self.events = store, events
        self._restart, self._start = restart, start
        self._running, self._wanted, self._adopt, self._clock = process_running, wanted, adopt, clock
        self._lock = threading.Lock()
        self._bad = 0
        self._last_msg: Optional[float] = None
        self._settle_until = 0.0
        self._last_restart: Optional[float] = None
        self._down_since: Optional[float] = None
        self._last_try: Optional[float] = None
        self._history: Deque[float] = deque()
        self._busy = False
        self._outage_logged = False
        self.state = DISABLED if not self.w["enabled"] else WAITING
        self.detail = ""
        self.restarts_total = 0
        self.starts_total = 0
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
            else:
                self._bad += 1

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        """Evaluate once (about once a second); may start a restart or a start."""
        if not self.enabled:
            self._publish()
            return
        now = self._clock()
        action = None
        with self._lock:
            running = self._running()
            if self._busy:
                self.state, self.detail = RESTARTING, "working on DSO"
            elif running:
                self._adopt()                      # a running DSO is a wanted DSO
                self._down_since = None
                self._outage_logged = False
                action = self._check_odometry(now)
            else:
                self._bad = 0
                action = self._check_down(now)
        if action:
            threading.Thread(target=self._do, args=(action,), name="dso-watchdog", daemon=True).start()
        self._publish()

    def _check_odometry(self, now: float) -> Optional[str]:
        if now < self._settle_until:
            self._bad = 0
            self.state, self.detail = SETTLING, f"DSO started, ignoring odometry for {self._settle_until - now:.0f} s"
            return None
        if self._bad >= int(self.w["bad_messages"]):
            while self._history and now - self._history[0] > float(self.w["window_s"]):
                self._history.popleft()
            fast = len(self._history) >= int(self.w["fast_restarts"])
            gap = float(self.w["retry_s"]) if fast else float(self.w["cooldown_s"])
            if self._last_restart is not None and now - self._last_restart < gap:
                self.state = BAD
                self.detail = f"odometry is NaN, next restart in {gap - (now - self._last_restart):.0f} s"
                return None
            self._busy = True
            self.state, self.detail = RESTARTING, "odometry is NaN, restarting DSO"
            return "restart"
        if self._last_msg is None:
            self.state, self.detail = WAITING, "no odometry received yet"
        else:
            self.state, self.detail = OK, "odometry is finite"
        return None

    def _check_down(self, now: float) -> Optional[str]:
        if not self._wanted():
            self._down_since = None
            self.state, self.detail = IDLE, "DSO is not running"
            return None
        if self._down_since is None:
            self._down_since = now
        since = max(self._down_since, self._last_try or 0.0)
        wait = float(self.w["retry_s"]) - (now - since)
        if wait > 0:
            self.state, self.detail = RETRYING, f"DSO is not running, trying again in {wait:.0f} s"
            return None
        self._busy = True
        self.state, self.detail = RESTARTING, "DSO is not running, starting it"
        return "start"

    def _do(self, action: str) -> None:
        if action == "restart":
            self.events.add("warning", "watchdog", "DSO odometry is NaN: restarting DSO",
                            "AIO NAV is not restarted")
        elif not self._outage_logged:
            self._outage_logged = True
            self.events.add("warning", "watchdog", "DSO is not running: starting it again",
                            f"trying every {float(self.w['retry_s']):.0f} s until it stays up")
        try:
            result = (self._restart if action == "restart" else self._start)()
        except Exception as e:  # keep the watchdog alive whatever the action does
            logger.exception("DSO %s failed", action)
            result = {"success": False, "summary": f"error: {e}"}
        now = self._clock()
        with self._lock:
            self._busy = False
            self._bad = 0
            self._last_try = now
            if action == "restart":
                self._last_restart = now
                self._history.append(now)
                self.restarts_total += 1
            else:
                self.starts_total += 1
            self._settle_until = now + float(self.w["settle_s"])
            self.last_restart_ts = time.time()
            verb = "restarted" if action == "restart" else "started"
            self.last_result = verb if result.get("success") else f"{action} failed"
            if result.get("summary"):
                self.last_result += ": " + result["summary"]
        self._publish()

    # ---------------------------------------------------------------- output
    def status(self) -> Dict:
        with self._lock:
            return {
                "enabled": self.enabled, "state": self.state, "detail": self.detail,
                "topic": self.w["topic"], "restarts": self.restarts_total, "starts": self.starts_total,
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
