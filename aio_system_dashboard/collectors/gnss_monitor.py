"""GNSS receiver quality from the NavSatFix topic: RTK fix, RTK float, SPP or no signal.

The OpenRTK330 driver reports the position type in ``NavSatFix.status.status``. A recorded
22-minute drive shows how it is used:

    status  2 (GBAS_FIX)  position sigma 2-33 cm    RTK fix
    status  1 (SBAS_FIX)  position sigma 8-10 cm    RTK float
    status  0 (FIX)       position sigma 10.0 m     SPP (single point)
    status -1 (NO_FIX)                              no position

``classify`` maps that to a quality; no message for ``timeout_s`` is "none" (no signal). This is
the receiver's quality, not whether the navigation filter used GNSS (that is the NAV packet's
GNSS flag, shown as the GNSS aiding lamp on the Navigation page).
"""

import logging
import math
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

FIXED, FLOAT, SPP, NONE = "fixed", "float", "spp", "none"
LABELS = {FIXED: "RTK fix", FLOAT: "RTK float", SPP: "SPP", NONE: "No signal"}
_BY_STATUS = {2: FIXED, 1: FLOAT, 0: SPP}


def classify(status: Optional[int]) -> str:
    return _BY_STATUS.get(status, NONE) if status is not None else NONE


def sigma_h(cov) -> Optional[float]:
    """Horizontal 1-sigma in metres from a NavSatFix position covariance (row-major 3x3)."""
    try:
        v = max(float(cov[0]), float(cov[4]))
    except (TypeError, ValueError, IndexError):
        return None
    return math.sqrt(v) if math.isfinite(v) and v >= 0 else None


class GnssMonitor:
    section = "gnss"

    def __init__(self, cfg, store, clock: Callable[[], float] = time.monotonic):
        self.g = cfg["gnss"]
        self.store, self._clock = store, clock
        self._lock = threading.Lock()
        self._status: Optional[int] = None
        self._sigma: Optional[float] = None
        self._last: Optional[float] = None
        self._count = 0

    def on_fix(self, status: int, cov) -> None:
        with self._lock:
            self._status, self._sigma = int(status), sigma_h(cov)
            self._last = self._clock()
            self._count += 1

    def snapshot(self) -> Dict:
        now = self._clock()
        with self._lock:
            age = None if self._last is None else now - self._last
            stale = age is None or age > float(self.g["timeout_s"])
            quality = NONE if stale else classify(self._status)
            return {"available": True, "quality": quality, "label": LABELS[quality],
                    "status": None if stale else self._status,
                    "sigma_h_m": None if stale else self._sigma,
                    "age_s": age, "topic": self.g["topic"], "messages": self._count}

    def tick(self) -> None:
        self.store.set(self.section, self.snapshot())

    def attach(self, node, qos) -> None:
        """Subscribe on the ROS monitor's node (NavSatFix is small and about 1 Hz)."""
        from sensor_msgs.msg import NavSatFix
        node.create_subscription(
            NavSatFix, self.g["topic"],
            lambda m: self.on_fix(m.status.status, m.position_covariance), qos)
        node.create_timer(1.0, self.tick)
        logger.info("GNSS quality from %s", self.g["topic"])
