"""Vehicle speed from the GNSS receiver, for "vehicle still" (state/still.py).

The OpenRTK330 driver publishes no plain GNSS velocity: NavSatFix has none, and the g1 packet's
velocity is not republished. Its nav_msgs/Odometry (/openrtk330/odometry, 100 Hz) carries the
receiver's own INS solution (i1 packet), whose velocity follows GNSS while the receiver has a
position. Without one it drifts or freezes: indoors on 2026-10-21 every message repeated the same
velocity to the last bit. So the speed is only used while the GNSS monitor reports a position
(state/still.py), and a speed whose values do not change at all is reported as frozen.

Like the IMU, the topic is listened to only during the ROS collector's sampling windows (a 100 Hz
topic costs about 12 % of a core in Python callbacks otherwise), and messages are not deserialized.
"""

import logging
import math
import struct
import threading
import time
from collections import deque
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

WINDOW_S = 2.0          # speeds judged together, like the IMU's MOTION_WINDOW_S
MIN_SAMPLES = 10


def odometry_velocity(raw) -> Optional[tuple]:
    """twist.twist.linear (x, y, z) of a serialized nav_msgs/Odometry.

    CDR after the 4-byte encapsulation: header.stamp (8 bytes), header.frame_id (u32 length incl.
    the NUL, then the characters), child_frame_id (aligned to 4, same form), padding to 8,
    pose.pose (7 doubles), pose.covariance (36 doubles), then twist.twist.linear (3 doubles)."""
    try:
        order = "<" if raw[1] & 1 else ">"
        d = memoryview(raw)[4:]
        n = struct.unpack_from(order + "I", d, 8)[0]
        pos = (12 + n + 3) & ~3
        m = struct.unpack_from(order + "I", d, pos)[0]
        p = (pos + 4 + m + 7) & ~7
        v = struct.unpack_from(order + "3d", d, p + 344)
    except (TypeError, struct.error, IndexError, ValueError):
        return None
    return v if all(math.isfinite(x) for x in v) else None


class SpeedMonitor:
    section = "speed"

    def __init__(self, cfg, store, clock: Callable[[], float] = time.monotonic):
        self.topic = (cfg["readiness"].get("still_speed_topic") or "").strip()
        self.store, self._clock = store, clock
        self._lock = threading.Lock()
        self._samples: deque = deque()            # (time, (vx, vy, vz)), the newest WINDOW_S
        self._count = 0
        self.window_open: Callable[[], Optional[bool]] = lambda: None   # set by the ROS collector
        self._node = self._qos = self._sub = None

    # ------------------------------------------------------------------ input
    def on_message(self, raw) -> None:
        v = odometry_velocity(raw)
        if v is None:
            return
        now = self._clock()
        with self._lock:
            self._count += 1
            self._samples.append((now, v))
            while self._samples and self._samples[0][0] < now - WINDOW_S:
                self._samples.popleft()

    # ----------------------------------------------------------------- output
    def snapshot(self) -> Dict:
        if not self.topic:
            return {"available": False, "reason": "no speed topic configured"}
        now = self._clock()
        with self._lock:
            s, count = list(self._samples), self._count
        if len(s) < MIN_SAMPLES:
            reason = (f"no message on {self.topic} yet" if not count else f"too few recent messages on {self.topic}")
            return {"available": False, "reason": reason, "topic": self.topic, "messages": count}
        # The same velocity to the last bit in every message is not a measurement (real ones have noise).
        frozen = len({v for _, v in s}) == 1
        speeds = [math.hypot(v[0], v[1]) for _, v in s]
        return {"available": True, "speed_max": round(max(speeds), 3), "frozen": frozen,
                "age_s": round(now - s[-1][0], 1), "samples": len(s), "topic": self.topic,
                "messages": count, "reason": "the receiver repeats the same speed (no GNSS solution)" if frozen else ""}

    def tick(self) -> None:
        self.store.set(self.section, self.snapshot())

    # ------------------------------------------------------------------- ROS
    def attach(self, node, qos) -> None:
        """Subscribe on the ROS monitor's node, raw, during its sampling windows."""
        if not self.topic:
            return
        from nav_msgs.msg import Odometry
        self._node, self._qos, self._cls = node, qos, Odometry
        node.create_timer(0.25, self._sync)
        node.create_timer(1.0, self.tick)
        logger.info("vehicle speed from %s", self.topic)

    def _sync(self) -> None:
        want = self.window_open()
        want = True if want is None else want
        if want and self._sub is None:
            self._sub = self._node.create_subscription(self._cls, self.topic, self.on_message, self._qos, raw=True)
        elif not want and self._sub is not None:
            try:
                self._node.destroy_subscription(self._sub)
            except Exception:
                logger.exception("could not end the speed listening window")
            self._sub = None
