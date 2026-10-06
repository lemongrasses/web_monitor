"""On-demand camera / LiDAR preview from ROS topics.

The dashboard subscribes to a preview topic only while a maintenance page is
requesting frames, and unsubscribes after ``idle_timeout_s`` without requests,
so it is not a permanent second consumer of heavy sensor data (spec §15).

Messages are received raw (no deserialization in the callback). The latest one
is deserialized and converted only when the browser asks for a frame (~2 Hz),
and the result is cached per message.
"""

import json
import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import numpy as np  # noqa: F401  (needed by the converters below)
    from .image import ImageConvertError, compressed_mime, encode_rgb, image_msg_to_array
    from .pointcloud import PointCloudError, pointcloud2_to_xyzi
    from .synthetic import fake_camera_frame, fake_lidar_scan
    NUMPY_ERROR = None
except ImportError as e:  # numpy missing: previews disabled, everything else works
    NUMPY_ERROR = str(e)

IMAGE_TYPES = ("sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage")
CLOUD_TYPES = ("sensor_msgs/msg/PointCloud2",)


class PreviewError(Exception):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


class _Source:
    def __init__(self, device: str, kind: str, cfg: Dict):
        self.device = device
        self.kind = kind  # image | pointcloud
        self.topic = cfg.get("topic", "")
        self.max_width = int(cfg.get("max_width", 960))
        self.max_points = int(cfg.get("max_points", 30000))
        self.last_request = 0.0
        self.sub = None
        self.msg_type: Optional[str] = None
        self.msg_cls = None
        self.raw: Optional[bytes] = None
        self.raw_time = 0.0
        self.seq = 0
        self.cache: Optional[Tuple[int, bytes, str, Dict]] = None
        self.status = "idle"


class PreviewTap:
    def __init__(self, cfg, fake: bool = False, idle_timeout_s: float = 10.0, fake_state=None):
        self.fake = fake
        self.fake_state = fake_state  # FakeState: lets demos show a missing/stale topic
        self.idle_timeout_s = idle_timeout_s
        self._lock = threading.Lock()
        self.sources: Dict[str, _Source] = {}
        for key, dev in cfg["devices"].items():
            prev = (dev or {}).get("preview")
            if prev and prev.get("kind") in ("image", "pointcloud"):
                self.sources[key] = _Source(key, prev["kind"], prev)

    def describe(self) -> Dict[str, Dict]:
        return {k: {"kind": s.kind, "topic": s.topic} for k, s in self.sources.items()}

    # ------------------------------------------------------------- ROS thread
    def sync(self, node, topic_types: Dict[str, list]) -> None:
        """Create/destroy preview subscriptions. Must run in the ROS executor thread."""
        now = time.monotonic()
        for s in self.sources.values():
            wanted = now - s.last_request < self.idle_timeout_s
            if wanted and s.sub is None:
                types = topic_types.get(s.topic)
                if not types:
                    s.status = "topic not found"
                    continue
                allowed = IMAGE_TYPES if s.kind == "image" else CLOUD_TYPES
                msg_type = next((t for t in types if t in allowed), None)
                if msg_type is None:
                    s.status = f"unsupported type {types[0]}"
                    continue
                try:
                    from rclpy.qos import qos_profile_sensor_data
                    from rosidl_runtime_py.utilities import get_message
                    s.msg_cls = get_message(msg_type)
                    s.msg_type = msg_type
                    s.sub = node.create_subscription(
                        s.msg_cls, s.topic, lambda raw, src=s: self._on_raw(src, raw),
                        qos_profile_sensor_data, raw=True)
                    s.status = "subscribed"
                    logger.info("preview: subscribed %s (%s)", s.topic, msg_type)
                except Exception as e:
                    s.status = f"subscribe failed: {e}"
                    logger.warning("preview subscribe %s failed: %s", s.topic, e)
            elif not wanted and s.sub is not None:
                node.destroy_subscription(s.sub)
                with self._lock:
                    s.sub, s.raw, s.cache = None, None, None
                s.status = "idle"
                logger.info("preview: unsubscribed %s (no viewers)", s.topic)

    def _on_raw(self, src: _Source, raw: bytes) -> None:
        with self._lock:
            src.raw = raw
            src.raw_time = time.monotonic()
            src.seq += 1

    # ------------------------------------------------------------ web threads
    def frame(self, device: str, ros_available: bool, ros_error: str = "") -> Tuple[bytes, str, Dict]:
        """Latest preview as (payload, content type, metadata). Raises PreviewError."""
        s = self.sources.get(device)
        if s is None:
            raise PreviewError("no preview configured for this device", 404)
        if NUMPY_ERROR:
            raise PreviewError(f"preview needs numpy ({NUMPY_ERROR})")
        s.last_request = time.monotonic()
        if self.fake:
            return self._fake_frame(s)
        if not ros_available:
            raise PreviewError(f"ROS 2 unavailable: {ros_error or 'starting'}")
        with self._lock:
            raw, seq, raw_time, cache = s.raw, s.seq, s.raw_time, s.cache
        if raw is None:
            raise PreviewError({"subscribed": f"waiting for messages on {s.topic}",
                                "idle": f"subscribing to {s.topic}…"}.get(s.status, f"{s.topic}: {s.status}"))
        age = time.monotonic() - raw_time
        if cache and cache[0] == seq:
            payload, ctype, meta = cache[1], cache[2], dict(cache[3])
        else:
            from rclpy.serialization import deserialize_message
            try:
                msg = deserialize_message(raw, s.msg_cls)
                payload, ctype, meta = self._convert(s, msg)
            except (ImageConvertError, PointCloudError) as e:
                raise PreviewError(str(e), 502)
            with self._lock:
                s.cache = (seq, payload, ctype, meta)
        meta["age_s"] = round(age, 3)
        return payload, ctype, meta

    def _convert(self, s: _Source, msg) -> Tuple[bytes, str, Dict]:
        frame_id = getattr(getattr(msg, "header", None), "frame_id", "")
        if s.kind == "image":
            if s.msg_type.endswith("CompressedImage"):
                return bytes(msg.data), compressed_mime(msg.format), {"frame": frame_id, "source": "compressed"}
            arr = image_msg_to_array(msg, s.max_width)
            payload, ctype = encode_rgb(arr)
            return payload, ctype, {"frame": frame_id, "size": f"{msg.width}x{msg.height}",
                                    "encoding": msg.encoding}
        pts = pointcloud2_to_xyzi(msg, s.max_points)
        return pts.tobytes(), "application/octet-stream", {
            "frame": frame_id, "count": len(pts), "total": int(msg.width) * int(msg.height)}

    def _fake_frame(self, s: _Source) -> Tuple[bytes, str, Dict]:
        if self.fake_state is not None:
            state = self.fake_state.topic(s.topic)["state"]
            if state in ("missing", "stale"):
                raise PreviewError(f"No messages on {s.topic} (topic {state})")
        if s.kind == "image":
            payload, ctype = encode_rgb(fake_camera_frame())
            return payload, ctype, {"frame": "camera_fake", "size": "640x400", "age_s": 0.03}
        pts = fake_lidar_scan()
        total = len(pts)
        if total > s.max_points:
            pts = pts[:: -(-total // s.max_points)]
        return pts.tobytes(), "application/octet-stream", {
            "frame": "os_sensor_fake", "count": len(pts), "total": total, "age_s": 0.05}


def meta_header(meta: Dict) -> str:
    return json.dumps(meta, separators=(",", ":"))
