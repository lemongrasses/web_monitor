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

* DSO uses too much memory. Its RAM plus swap is checked every tick; above ``max_memory_mb``
  (auto: 20% of RAM) DSO is restarted at once. DSO was seen keeping every camera frame (about
  25 MB/s) until RAM and swap were full and the whole machine hung; restarting DSO in time keeps
  the rest (AIO NAV, the dashboard, the desktop) alive.

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
                 adopt: Callable[[], None], clock: Callable[[], float] = time.monotonic,
                 memory_mb: Optional[Callable[[], Optional[float]]] = None,
                 total_memory_mb: Optional[float] = None):
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
        self._memory = memory_mb
        self.memory_mb: Optional[float] = None
        self.memory_restarts = 0
        limit = self.w.get("max_memory_mb", "auto")
        if limit in (None, "auto"):
            limit = 0.2 * total_memory_mb if total_memory_mb else None
        self.memory_limit_mb: Optional[float] = float(limit) if limit else None   # 0 / None: off

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
                action = self._check_memory() or self._check_odometry(now)
            else:
                self._bad = 0
                action = self._check_down(now)
        if action:
            threading.Thread(target=self._do, args=(action,), name="dso-watchdog", daemon=True).start()
        self._publish()

    def _check_memory(self) -> Optional[str]:
        self.memory_mb = self._memory() if self._memory else None
        if not self.memory_limit_mb or self.memory_mb is None or self.memory_mb <= self.memory_limit_mb:
            return None
        self._busy = True
        self.state, self.detail = RESTARTING, (f"DSO uses {self.memory_mb:.0f} MB (limit "
                                               f"{self.memory_limit_mb:.0f} MB), restarting DSO")
        return "restart_memory"

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
        elif action == "restart_memory":
            self.events.add("fault", "watchdog", "DSO used too much memory: restarting DSO",
                            f"{self.memory_mb:.0f} MB of RAM and swap, limit {self.memory_limit_mb:.0f} MB "
                            "(dso_watchdog.max_memory_mb); AIO NAV is not restarted")
        elif not self._outage_logged:
            self._outage_logged = True
            self.events.add("warning", "watchdog", "DSO is not running: starting it again",
                            f"trying every {float(self.w['retry_s']):.0f} s until it stays up")
        try:
            result = (self._start if action == "start" else self._restart)()
        except Exception as e:  # keep the watchdog alive whatever the action does
            logger.exception("DSO %s failed", action)
            result = {"success": False, "summary": f"error: {e}"}
        now = self._clock()
        with self._lock:
            self._busy = False
            self._bad = 0
            self._last_try = now
            if action in ("restart", "restart_memory"):
                self._last_restart = now
                self._history.append(now)
                self.restarts_total += 1
                if action == "restart_memory":
                    self.memory_restarts += 1
            else:
                self.starts_total += 1
            self._settle_until = now + float(self.w["settle_s"])
            self.last_restart_ts = time.time()
            verb = "started" if action == "start" else "restarted"
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
                "memory_mb": None if self.memory_mb is None else round(self.memory_mb),
                "memory_limit_mb": None if self.memory_limit_mb is None else round(self.memory_limit_mb),
                "memory_restarts": self.memory_restarts,
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
