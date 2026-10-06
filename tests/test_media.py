import struct
import time
import unittest
import zlib
from types import SimpleNamespace as NS

import numpy as np

from aio_system_dashboard.media.image import (
    ImageConvertError, encode_png, image_msg_to_array)
from aio_system_dashboard.media.pointcloud import PointCloudError, pointcloud2_to_xyzi
from aio_system_dashboard.media.tap import PreviewError, PreviewTap


def field(name, offset, datatype=7):
    return NS(name=name, offset=offset, datatype=datatype, count=1)


def cloud(points, intensity_u16=False, big=False, pad=0):
    """Build a PointCloud2-like message: x,y,z float32 (+ intensity) with optional padding."""
    order = ">" if big else "<"
    point_step = 16 + pad
    data = b""
    for x, y, z, i in points:
        rec = struct.pack(order + "fff", x, y, z)
        rec += struct.pack(order + ("H2x" if intensity_u16 else "f"), int(i) if intensity_u16 else i)
        data += rec + b"\0" * pad
    fields = [field("x", 0), field("y", 4), field("z", 8),
              field("intensity", 12, 4 if intensity_u16 else 7)]
    return NS(fields=fields, is_bigendian=big, point_step=point_step, width=len(points), height=1,
              row_step=point_step * len(points), data=data)


class PointCloudTest(unittest.TestCase):
    def test_decode_and_filter(self):
        pts = [(1, 2, 3, 10), (float("nan"), 0, 0, 1), (0, 0, 0, 5), (-4, 5, -1, 20)]
        for kw in ({}, {"big": True}, {"pad": 8}, {"intensity_u16": True}):
            out = pointcloud2_to_xyzi(cloud(pts, **kw))
            self.assertEqual(out.dtype, np.float32)
            np.testing.assert_allclose(out, [[1, 2, 3, 10], [-4, 5, -1, 20]], err_msg=str(kw))

    def test_subsample(self):
        pts = [(i + 1.0, 0, 0, 0) for i in range(1000)]
        self.assertEqual(len(pointcloud2_to_xyzi(cloud(pts), max_points=100)), 100)

    def test_missing_xyz(self):
        msg = cloud([(1, 1, 1, 1)])
        msg.fields = msg.fields[1:]
        with self.assertRaises(PointCloudError):
            pointcloud2_to_xyzi(msg)


def image(enc, w, h, data, step):
    return NS(encoding=enc, width=w, height=h, step=step, data=data, is_bigendian=0)


class ImageTest(unittest.TestCase):
    def test_rgb_bgr(self):
        px = bytes([10, 20, 30]) * 4
        rgb = image_msg_to_array(image("rgb8", 2, 2, px, 6))
        bgr = image_msg_to_array(image("bgr8", 2, 2, px, 6))
        self.assertEqual(rgb.shape, (2, 2, 3))
        self.assertEqual(list(rgb[0, 0]), [10, 20, 30])
        self.assertEqual(list(bgr[0, 0]), [30, 20, 10])

    def test_downscale_and_step_padding(self):
        w, h, step = 2000, 10, 2004
        data = bytes(step * h)
        out = image_msg_to_array(image("mono8", w, h, data, step), max_width=960)
        self.assertEqual(out.shape, (4, 667))  # stride 3

    def test_mono16_stretch(self):
        arr = np.arange(16, dtype="<u2").reshape(4, 4) * 100
        out = image_msg_to_array(image("mono16", 4, 4, arr.tobytes(), 8))
        self.assertEqual(out.dtype, np.uint8)
        self.assertEqual(out.max(), 255)

    def test_unsupported(self):
        with self.assertRaises(ImageConvertError):
            image_msg_to_array(image("32FC1", 1, 1, bytes(4), 4))

    def test_png(self):
        img = np.random.default_rng(0).integers(0, 255, (5, 7, 3), dtype=np.uint8)
        png = encode_png(img)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        rows = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(5, 1 + 7 * 3)
        np.testing.assert_array_equal(rows[:, 1:].reshape(5, 7, 3), img)


class FakeNode:
    def __init__(self):
        self.created, self.destroyed = [], []

    def create_subscription(self, cls, topic, cb, qos, raw=False):
        self.created.append(topic)
        return (topic, cb)

    def destroy_subscription(self, sub):
        self.destroyed.append(sub[0])


CFG = {"devices": {
    "camera": {"preview": {"kind": "image", "topic": "/cam/compressed"}},
    "lidar": {"preview": {"kind": "pointcloud", "topic": "/points"}},
    "other": {"host": "1.2.3.4"},
}}


class TapTest(unittest.TestCase):
    def setUp(self):
        import sys
        sys.modules.setdefault("rclpy", NS())
        sys.modules.setdefault("rclpy.qos", NS(qos_profile_sensor_data=None))
        sys.modules.setdefault("rosidl_runtime_py", NS())
        sys.modules.setdefault("rosidl_runtime_py.utilities", NS(get_message=lambda t: object))

    def test_subscribes_only_while_requested(self):
        tap = PreviewTap(CFG, idle_timeout_s=0.2)
        self.assertEqual(set(tap.describe()), {"camera", "lidar"})
        node = FakeNode()
        topics = {"/cam/compressed": ["sensor_msgs/msg/CompressedImage"],
                  "/points": ["sensor_msgs/msg/PointCloud2"]}
        tap.sync(node, topics)
        self.assertEqual(node.created, [])  # nobody watching
        with self.assertRaises(PreviewError):
            tap.frame("camera", ros_available=True)  # marks interest, no data yet
        tap.sync(node, topics)
        self.assertEqual(node.created, ["/cam/compressed"])
        time.sleep(0.25)
        tap.sync(node, topics)
        self.assertEqual(node.destroyed, ["/cam/compressed"])

    def test_wrong_type_and_missing_topic(self):
        tap = PreviewTap(CFG)
        node = FakeNode()
        tap.sources["lidar"].last_request = time.monotonic()
        tap.sync(node, {"/points": ["sensor_msgs/msg/LaserScan"]})
        self.assertIn("unsupported type", tap.sources["lidar"].status)
        tap.sync(node, {})
        self.assertEqual(tap.sources["lidar"].status, "topic not found")
        self.assertEqual(node.created, [])

    def test_ros_frame_converts_once_per_message(self):
        import sys
        calls = []
        msg = NS(header=NS(frame_id="cam"), format="jpeg", data=b"\xff\xd8jpegdata")
        sys.modules["rclpy.serialization"] = NS(
            deserialize_message=lambda raw, cls: calls.append(raw) or msg)
        tap = PreviewTap(CFG)
        src = tap.sources["camera"]
        src.msg_type, src.msg_cls = "sensor_msgs/msg/CompressedImage", object
        tap._on_raw(src, b"raw-1")
        payload, ctype, meta = tap.frame("camera", ros_available=True)
        tap.frame("camera", ros_available=True)
        self.assertEqual((payload, ctype, meta["frame"]), (b"\xff\xd8jpegdata", "image/jpeg", "cam"))
        self.assertEqual(calls, [b"raw-1"])  # cached until a new message arrives
        tap._on_raw(src, b"raw-2")
        tap.frame("camera", ros_available=True)
        self.assertEqual(calls, [b"raw-1", b"raw-2"])
        with self.assertRaises(PreviewError):
            tap.frame("camera", ros_available=False, ros_error="rclpy missing")

    def test_unknown_device(self):
        with self.assertRaises(PreviewError) as cm:
            PreviewTap(CFG).frame("other", ros_available=True)
        self.assertEqual(cm.exception.status, 404)

    def test_fake_frames(self):
        tap = PreviewTap(CFG, fake=True)
        img, ctype, _ = tap.frame("camera", ros_available=False)
        self.assertIn(ctype, ("image/jpeg", "image/png"))
        self.assertGreater(len(img), 1000)
        pts, ctype, meta = tap.frame("lidar", ros_available=False)
        self.assertEqual(ctype, "application/octet-stream")
        self.assertEqual(len(pts), meta["count"] * 16)


if __name__ == "__main__":
    unittest.main()
