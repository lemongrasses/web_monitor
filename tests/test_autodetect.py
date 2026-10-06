import os
import tempfile
import unittest
from unittest import mock

from aio_system_dashboard import config as config_mod
from aio_system_dashboard.collectors.ros2 import pick_topic
from aio_system_dashboard.config import aio_nav_output_dir, load_config

GRAPH = {
    "/imu/raw": ["sensor_msgs/msg/Imu"],
    "/ouster/imu": ["sensor_msgs/msg/Imu"],
    "/ouster/points": ["sensor_msgs/msg/PointCloud2"],
    "/camera/depth/image_raw/compressedDepth": ["sensor_msgs/msg/CompressedImage"],
    "/camera/color/image_raw": ["sensor_msgs/msg/Image"],
    "/camera/color/image_raw/compressed": ["sensor_msgs/msg/CompressedImage"],
    "/nav/odometry": ["nav_msgs/msg/Odometry"],
}


class PickTopicTest(unittest.TestCase):
    def test_prefers_compressed_and_avoids_depth(self):
        types = ("sensor_msgs/msg/CompressedImage", "sensor_msgs/msg/Image")
        self.assertEqual(pick_topic(GRAPH, types), "/camera/color/image_raw/compressed")

    def test_hint_and_strict(self):
        self.assertEqual(pick_topic(GRAPH, "sensor_msgs/msg/Imu", "ouster"), "/ouster/imu")
        no_ouster = {k: v for k, v in GRAPH.items() if "ouster" not in k}
        # loose hint falls back to another IMU; strict hint does not
        self.assertEqual(pick_topic(no_ouster, "sensor_msgs/msg/Imu", "ouster"), "/imu/raw")
        self.assertIsNone(pick_topic(no_ouster, "sensor_msgs/msg/Imu", "ouster", strict=True))

    def test_none_when_absent(self):
        self.assertIsNone(pick_topic(GRAPH, "sensor_msgs/msg/LaserScan"))


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.join(self.tmp.name, "home", "jetson", "aio-nav-ros")
        self.cfg_dir = os.path.join(root, "install", "aio_nav_ros", "share", "aio_nav_ros", "config")
        os.makedirs(self.cfg_dir)
        self.yaml = os.path.join(self.cfg_dir, "aio_nav.yaml")
        with open(self.yaml, "w") as f:
            f.write("aio_nav_node:\n  ros__parameters:\n    output_udp: '192.168.1.5:9000'\n"
                    "    output_rate: 50.0\n")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_output_dir_next_to_install(self):
        self.assertEqual(aio_nav_output_dir(self.yaml), os.path.join(self.root, "output"))
        self.assertEqual(aio_nav_output_dir(self.yaml, "/data/logs/run_fusion.txt"), "/data/logs")

    def test_auto_config_and_data_root(self):
        globs = (os.path.join(self.tmp.name, "home", "*", "aio-nav-ros", "install", "aio_nav_ros",
                              "share", "aio_nav_ros", "config", "aio_nav.yaml"),)
        with mock.patch.object(config_mod, "AIO_NAV_CONFIG_GLOBS", globs), \
                mock.patch.dict(os.environ, {"AIO_NAV_CONFIG": ""}):
            cfg = load_config("config/dashboard.yaml")
        self.assertTrue(cfg.aio_nav["loaded"])
        self.assertTrue(cfg.aio_nav["auto"])
        self.assertEqual(cfg.expected_nav_rate, 50.0)
        self.assertEqual(cfg.nav_destinations()[0]["host"], "192.168.1.5")
        self.assertEqual(cfg["data"]["roots"][0]["path"], os.path.join(self.root, "output"))

    def test_nothing_found(self):
        with mock.patch.object(config_mod, "AIO_NAV_CONFIG_GLOBS", ()), \
                mock.patch.dict(os.environ, {"AIO_NAV_CONFIG": ""}):
            cfg = load_config("config/dashboard.yaml")
        self.assertFalse(cfg.aio_nav["loaded"])
        self.assertIn("not found", cfg.aio_nav["error"])
        self.assertEqual(cfg["data"]["roots"], [])


if __name__ == "__main__":
    unittest.main()
