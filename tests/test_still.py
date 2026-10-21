"""Vehicle still: receiver speed, the judgement, the limits and measuring them (state/still.py)."""

import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from aio_system_dashboard.collectors.speed import SpeedMonitor, odometry_velocity
from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.state import still

import copy


def odom_cdr(v, frame="odom_link", child="map", little=True) -> bytes:
    """A serialized nav_msgs/Odometry with twist.twist.linear = v."""
    o = "<" if little else ">"

    def string(s):
        b = s.encode() + b"\0"
        return struct.pack(o + "I", len(b)) + b

    d = bytearray(struct.pack(o + "iI", 1792551615, 990000009)) + string(frame)
    while len(d) % 4:
        d += b"\0"
    d += string(child)
    while len(d) % 8:
        d += b"\0"
    d += struct.pack(o + "7d", 1, 2, 3, 0, 0, 0.7, 0.7) + struct.pack(o + "36d", *[9.0] * 36)
    d += struct.pack(o + "3d", *v) + struct.pack(o + "3d", 5, 5, 5) + struct.pack(o + "36d", *[9.0] * 36)
    return bytes([0, 1 if little else 0, 0, 0]) + bytes(d)


class Events:
    def __init__(self):
        self.titles = []

    def add(self, level, category, title, detail=""):
        self.titles.append(title)


def cfg():
    return Config(copy.deepcopy(DEFAULTS), None)


class SpeedTest(unittest.TestCase):
    def test_velocity_from_raw_odometry(self):
        for frame, child, little in (("odom_link", "map", True), ("", "", True), ("abc", "base_link_x", False)):
            self.assertEqual(odometry_velocity(odom_cdr((1.5, -2.0, 0.25), frame, child, little)), (1.5, -2.0, 0.25))
        self.assertIsNone(odometry_velocity(b"\x00\x01\x00\x00short"))
        self.assertIsNone(odometry_velocity(odom_cdr((float("nan"), 0, 0))))

    def test_matches_ros_serialization(self):
        try:
            from nav_msgs.msg import Odometry
            from rclpy.serialization import serialize_message
        except ImportError:
            self.skipTest("ROS 2 not sourced")
        m = Odometry()
        m.header.frame_id, m.child_frame_id = "odom_link", "map"
        m.twist.twist.linear.x, m.twist.twist.linear.y, m.twist.twist.linear.z = -0.0241, -0.0168, 0.5
        self.assertEqual(odometry_velocity(serialize_message(m)), (-0.0241, -0.0168, 0.5))

    def test_horizontal_speed_and_a_frozen_receiver(self):
        t = [100.0]
        mon = SpeedMonitor(cfg(), store=None, clock=lambda: t[0])
        self.assertFalse(mon.snapshot()["available"])
        for i in range(20):
            t[0] += 0.01
            mon.on_message(odom_cdr((3.0 + i * 1e-4, 4.0, 9.0)))       # 5 m/s horizontal; up is ignored
        s = mon.snapshot()
        self.assertEqual((s["available"], s["frozen"]), (True, False))
        self.assertAlmostEqual(s["speed_max"], 5.0, places=2)
        t[0] += 5                                                          # next window: indoors
        for _ in range(20):
            t[0] += 0.01
            mon.on_message(odom_cdr((-0.0241, -0.0168, -0.0054)))         # the same to the last bit
        self.assertTrue(mon.snapshot()["frozen"])


MOTION = {"gyro_max": 0.01, "accel_std": 0.04, "age_s": 0.2}
SPEED = {"available": True, "speed_max": 0.05, "frozen": False, "age_s": 0.2}
LIMITS = {"gyro_rad_s": 0.05, "accel_std": 0.15, "speed_m_s": 0.3}


class JudgeTest(unittest.TestCase):
    def test_imu_and_speed(self):
        fix = {"quality": "fixed"}
        self.assertTrue(still.judge(MOTION, SPEED, fix, LIMITS)["still"])
        self.assertIsNone(still.judge(None, SPEED, fix, LIMITS)["still"])
        j = still.judge(MOTION, dict(SPEED, speed_max=1.2), fix, LIMITS)
        self.assertEqual((j["still"], j["reason"], j["speed_used"]), (False, "moving: GNSS speed 1.20 m/s", True))
        for gnss, speed in (({"quality": "none"}, dict(SPEED, speed_max=1.2)), (None, dict(SPEED, speed_max=1.2)),
                            (fix, dict(SPEED, speed_max=1.2, frozen=True)), (fix, dict(SPEED, speed_max=1.2, age_s=30)),
                            (fix, {"available": False, "reason": "no message"})):
            j = still.judge(MOTION, speed, gnss, LIMITS)
            self.assertEqual((j["still"], j["speed_used"]), (True, False), (gnss, speed))
            self.assertTrue(j["speed_note"])
        self.assertIn("rotation", still.judge(dict(MOTION, gyro_max=0.2), SPEED, fix, LIMITS)["reason"])
        self.assertIn("acceleration", still.judge(dict(MOTION, accel_std=0.2), SPEED, fix, LIMITS)["reason"])


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state" / "still_limits.json"
        self.events = Events()

    def test_defaults_apply_reload_reset(self):
        lim = still.StillLimits(cfg(), self.path, self.events)
        self.assertEqual(lim.get()["how"], "default")
        self.assertEqual(lim.get()["accel_std"], 0.15)
        lim.apply({"gyro_rad_s": 0.07, "accel_std": 0.32, "speed_m_s": 0.2}, "measured", "192.0.2.5",
                  {"gyro_rad_s": 0.035, "accel_std": 0.16, "speed_m_s": 0.05})
        again = still.StillLimits(cfg(), self.path, self.events)           # a restart or a reboot
        self.assertEqual((again.get()["accel_std"], again.get()["how"], again.get()["by"]), (0.32, "measured", "192.0.2.5"))
        self.assertIn("Still limits changed", self.events.titles)
        again.reset("192.0.2.5")
        self.assertFalse(self.path.exists())
        self.assertEqual(still.StillLimits(cfg(), self.path).get()["how"], "default")

    def test_out_of_range_or_broken_values_are_refused(self):
        lim = still.StillLimits(cfg(), self.path, self.events)
        for bad in ({"gyro_rad_s": 0.5, "accel_std": 0.1, "speed_m_s": 0.2},     # driving would count as still
                    {"gyro_rad_s": 0.05, "accel_std": "x", "speed_m_s": 0.2},
                    {"gyro_rad_s": 0.05, "accel_std": 0.1}, [1, 2, 3], None):
            with self.assertRaises(ValueError):
                lim.apply(bad, "typed")
        self.assertEqual(lim.get()["how"], "default")
        self.path.parent.mkdir(parents=True)
        for text in ("{not json", json.dumps({"gyro_rad_s": 9, "accel_std": 0.1, "speed_m_s": 0.2})):
            self.path.write_text(text)
            self.assertEqual(still.StillLimits(cfg(), self.path, self.events).get()["how"], "default")
        self.assertIn("Still limits not usable", self.events.titles)


class FakeInputs:
    """IMU windows that differ a little each time (the measurement skips a window read twice)."""

    def __init__(self, gyro=0.01, accel=0.04, speed=0.04, gnss="fixed", age=0.2):
        self.gyro, self.accel, self.speed, self.gnss, self.age = gyro, accel, speed, gnss, age
        self.n = 0

    def __call__(self):
        self.n += 1
        wobble = (self.n % 5) * 0.001
        motion = {"gyro_max": self.gyro + wobble, "accel_std": self.accel + wobble, "samples": self.n, "age_s": self.age}
        speed = None if self.speed is None else {"available": True, "speed_max": self.speed, "frozen": False, "age_s": 0.2}
        return motion, speed, {"quality": self.gnss}


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.limits = still.StillLimits(cfg(), Path(self.tmp.name) / "still_limits.json")
        self.t = [0.0]
        self.lock = threading.Lock()

    def clock(self):
        with self.lock:
            self.t[0] += 0.1
            return self.t[0]

    def run_cal(self, cal, kind):
        cal.start(kind)
        deadline = time.monotonic() + 10
        while cal.status()["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        return cal.status()

    def make(self, inputs):
        return still.StillCalibration(inputs, self.limits, clock=self.clock, poll_s=0.001)

    def test_still_proposes_twice_the_largest_within_the_range(self):
        inputs = FakeInputs(gyro=0.03, accel=0.2, speed=0.04)
        st = self.run_cal(self.make(inputs), "still")
        self.assertEqual(st["state"], "done", st["message"])
        self.assertAlmostEqual(st["result"]["measured"]["gyro_rad_s"], 0.034)
        self.assertEqual(st["proposal"], {"gyro_rad_s": 0.068, "accel_std": 0.408, "speed_m_s": 0.15})  # speed floor
        self.assertIn("Proposed limits", st["message"])
        rough = self.run_cal(self.make(FakeInputs(accel=0.4)), "still")       # a rough idle: at the highest
        self.assertEqual(rough["proposal"]["accel_std"], 0.5)
        self.assertIn("highest allowed", rough["message"])

    def test_still_without_gnss_keeps_the_speed_limit(self):
        st = self.run_cal(self.make(FakeInputs(gnss="none")), "still")
        self.assertEqual(st["proposal"]["speed_m_s"], self.limits.get()["speed_m_s"])
        self.assertIn("GNSS speed was not available", st["message"])

    def test_a_vehicle_that_moves_fails_the_measurement(self):
        st = self.run_cal(self.make(FakeInputs(gyro=0.3)), "still")
        self.assertEqual(st["state"], "failed")
        self.assertIn("moved", st["message"])
        self.assertIsNone(st["proposal"])
        st = self.run_cal(self.make(FakeInputs(speed=0.9)), "still")         # GNSS speed says it rolled
        self.assertEqual(st["state"], "failed")

    def test_stale_imu_gives_no_readings(self):
        st = self.run_cal(self.make(FakeInputs(age=5.0)), "still")
        self.assertEqual(st["state"], "failed")
        self.assertIn("Only 0 IMU readings", st["message"])

    def test_check_while_driving(self):
        inputs = FakeInputs()
        cal = self.make(inputs)
        self.run_cal(cal, "still")
        inputs.gyro, inputs.accel, inputs.speed = 0.25, 0.8, 1.4                      # clearly driving
        st = self.run_cal(cal, "moving")
        self.assertEqual((st["state"], st["result"]["counted_still"], st["result"]["which"]), ("done", 0, "proposed"))
        inputs.gyro, inputs.accel = 0.01, 0.03                                        # smooth and straight ...
        st = self.run_cal(cal, "moving")
        self.assertEqual(st["result"]["counted_still"], 0)                            # ... the speed tells
        inputs.gnss = "none"
        st = self.run_cal(cal, "moving")                                              # ... without GNSS it cannot
        self.assertEqual(st["state"], "warning")
        self.assertIn("was the vehicle moving", st["message"])

    def test_one_at_a_time_cancel_and_no_imu(self):
        cal = still.StillCalibration(FakeInputs(), self.limits, poll_s=0.01)         # real clock: 60 s
        cal.start("still")
        with self.assertRaises(RuntimeError):
            cal.start("moving")
        cal.cancel()
        deadline = time.monotonic() + 5
        while cal.status()["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(cal.status()["state"], "cancelled")
        with self.assertRaises(ValueError):
            still.StillCalibration(lambda: (None, None, None), self.limits).start("still")
        with self.assertRaises(ValueError):
            cal.start("sideways")


if __name__ == "__main__":
    unittest.main()
