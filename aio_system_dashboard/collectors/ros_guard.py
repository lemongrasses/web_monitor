"""Notice when the dashboard's own ROS connection is stuck, and restart the dashboard.

Seen after a boot: the dashboard started before the sensor drivers, and its ROS discovery never
recovered. It then saw only its own topics and no node at all, so the camera and IMU looked
missing and the GNSS lamp showed no signal, although the drivers were running fine and a new
process in the same ROS domain saw everything.

A graph with no other nodes is normal when the drivers are off, so the guard only acts when the
OS says otherwise: the sensor driver process has been running for a while **in the same ROS domain
and localhost setting as the dashboard** and still no other node is visible. After ``grace_s`` of
that it asks for a restart (systemd starts the dashboard again). A restart is allowed at most
once per ``cooldown_s`` (kept on disk), so a wrong guess cannot turn into a restart loop.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def same_ros_setup(pid: int, environ: Optional[dict] = None) -> bool:
    """Does the process use the same ROS domain / localhost-only setting as this process?"""
    env = os.environ if environ is None else environ
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    theirs = dict(item.decode(errors="replace").split("=", 1) for item in raw if b"=" in item)
    return all(theirs.get(k, "") == env.get(k, "") for k in ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY"))


class RosIsolationGuard:
    def __init__(self, cfg, events, driver_pids: Callable[[], list], restart: Callable[[], None],
                 state_file: Path, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, same_setup: Callable[[int], bool] = same_ros_setup):
        r = cfg["ros"]
        self.enabled = bool(r.get("isolation_restart"))
        self.grace_s = float(r.get("isolation_grace_s", 30))
        self.cooldown_s = float(r.get("isolation_cooldown_s", 600))
        self.min_uptime_s = 60.0
        self.events, self._driver_pids, self._restart = events, driver_pids, restart
        self._state_file, self._clock, self._wall, self._same = state_file, clock, wall, same_setup
        self._started = clock()
        self._since: Optional[float] = None

    def _recently_restarted(self) -> bool:
        try:
            last = float(json.loads(self._state_file.read_text(encoding="utf-8")).get("ts", 0))
        except (OSError, ValueError, AttributeError):
            return False
        return self._wall() - last < self.cooldown_s

    def update(self, foreign_nodes: int) -> bool:
        """Call after every graph refresh. Returns True when it asked for a restart."""
        if not self.enabled:
            return False
        now = self._clock()
        pids = self._driver_pids() if foreign_nodes == 0 else []
        if foreign_nodes > 0 or not pids or not any(self._same(p) for p in pids):
            self._since = None
            return False
        if self._since is None:
            self._since = now
        if now - self._since < self.grace_s or now - self._started < self.min_uptime_s:
            return False
        if self._recently_restarted():
            return False
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({"ts": self._wall()}) + "\n", encoding="utf-8")
        except OSError as e:
            logger.warning("cannot record the restart, not restarting: %s", e)
            return False
        self.events.add("warning", "system", "ROS connection looks stuck: restarting the dashboard",
                        "The sensor drivers are running in this ROS domain but no node is visible.")
        self._since = None
        self._restart()
        return True
