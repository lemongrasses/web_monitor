"""GNSS receiver quality from the NavSatFix topic: RTK fix, RTK float, SPP or no signal.

The OpenRTK330 driver reports the position type in ``NavSatFix.status.status``. A recorded
22-minute drive shows how it is used:

    status  2 (GBAS_FIX)  position sigma 2-33 cm    RTK fix
    status  1 (SBAS_FIX)  position sigma 8-170 cm   RTK float
    status  0 (FIX)       position sigma 10.0 m     SPP (single point, a fixed 10 m from the driver)
    status -1 (NO_FIX)    position sigma 9-14 cm    !! a valid position: single epochs inside float
                                                    periods, the position continuing smoothly

So -1 does not mean "no position" for this driver (it seems to use it for solution types it does
not map). The status alone is therefore not trusted: a message with a usable position (finite
latitude / longitude, a known covariance) and an unrated status is "unrated", shown as
"Fix (type unknown)", amber when better than ``UNRATED_GOOD_M`` and red otherwise. "No signal" is
kept for no usable position: no message for ``timeout_s``, or a status without a position.

This is the receiver's quality, not whether the navigation filter used GNSS (that is the NAV
packet's GNSS flag, shown as the GNSS aiding lamp on the Navigation page).
"""

import logging
import math
import threading
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

FIXED, FLOAT, SPP, NONE = "fixed", "float", "spp", "none"
UNRATED, UNRATED_POOR = "unrated", "unrated_poor"      # a position, but the status does not say what kind
LABELS = {FIXED: "RTK fix", FLOAT: "RTK float", SPP: "SPP", NONE: "No signal",
          UNRATED: "Fix (type unknown)", UNRATED_POOR: "Fix (type unknown)"}
_BY_STATUS = {2: FIXED, 1: FLOAT, 0: SPP}
UNRATED_GOOD_M = 1.0                                   # unrated fixes at least this good are amber


def has_position(lat, lon, cov_type, sigma) -> bool:
    """A usable position: finite, in range, not 0/0, with a known covariance."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    return cov_type not in (0, None) and sigma is not None and sigma > 0


def classify(status: Optional[int], sigma: Optional[float] = None, position: bool = False) -> str:
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    if position:
        return UNRATED if sigma is not None and sigma <= UNRATED_GOOD_M else UNRATED_POOR
    return NONE


def sigma_h(cov) -> Optional[float]:
    """Horizontal 1-sigma in metres from a NavSatFix position covariance (row-major 3x3)."""
    try:
        v = max(float(cov[0]), float(cov[4]))
    except (TypeError, ValueError, IndexError):
        return None
    return math.sqrt(v) if math.isfinite(v) and v >= 0 else None


def _duration(s: Optional[float]) -> str:
    if s is None:
        return "-"
    if s < 120:
        return f"{s:.0f} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    return f"{s / 3600:.1f} h"


class GnssMonitor:
    section = "gnss"

    def __init__(self, cfg, store, clock: Callable[[], float] = time.monotonic):
        self.g = cfg["gnss"]
        self.store, self._clock = store, clock
        self._lock = threading.Lock()
        self._status: Optional[int] = None
        self._sigma: Optional[float] = None
        self._position = False
        self._last: Optional[float] = None
        self._count = 0

    def on_fix(self, status: int, cov, cov_type: Optional[int] = None, lat=None, lon=None) -> None:
        sigma = sigma_h(cov)
        with self._lock:
            self._status, self._sigma = int(status), sigma
            self._position = has_position(lat, lon, cov_type, sigma)
            self._last = self._clock()
            self._count += 1

    def snapshot(self) -> Dict:
        now = self._clock()
        with self._lock:
            age = None if self._last is None else now - self._last
            stale = age is None or age > float(self.g["timeout_s"])
            quality = NONE if stale else classify(self._status, self._sigma, self._position)
            if self._count == 0:
                reason = f"no GNSS message received yet on {self.g['topic']}"
            elif stale:
                reason = f"no GNSS message for {_duration(age)} (the receiver driver sends none)"
            elif quality == NONE:
                reason = f"the receiver reports no position (status {self._status})"
            else:
                reason = ""
            return {"available": True, "quality": quality, "label": LABELS[quality],
                    "status": None if stale else self._status,
                    "sigma_h_m": None if stale else self._sigma,
                    "age_s": age, "topic": self.g["topic"], "messages": self._count, "reason": reason}

    def tick(self) -> None:
        self.store.set(self.section, self.snapshot())

    def attach(self, node, qos) -> None:
        """Subscribe on the ROS monitor's node (NavSatFix is small and about 1 Hz)."""
        from sensor_msgs.msg import NavSatFix
        node.create_subscription(
            NavSatFix, self.g["topic"],
            lambda m: self.on_fix(m.status.status, m.position_covariance, m.position_covariance_type,
                                  m.latitude, m.longitude), qos)
        node.create_timer(1.0, self.tick)
        logger.info("GNSS quality from %s", self.g["topic"])
