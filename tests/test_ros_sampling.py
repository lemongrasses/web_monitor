import unittest
from unittest import mock

from aio_system_dashboard.collectors import ros2
from aio_system_dashboard.collectors.ros2 import _TopicStats, in_sampling_window


class WindowTest(unittest.TestCase):
    def test_each_period_starts_with_a_listening_window(self):
        f = lambda t: in_sampling_window(t, 100.0, 5.0, 1.5)
        self.assertEqual([f(100.0), f(101.4), f(101.6), f(104.9), f(105.0), f(106.4), f(106.6)],
                         [True, True, False, False, True, True, False])


class SampledStatsTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        patch = mock.patch.object(ros2.time, "monotonic", lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.s = _TopicStats(2.0)

    def window(self, hz=100.0, seconds=1.5):
        """Listen for `seconds`, receiving `hz` messages per second (0 = silence)."""
        self.s.start_window()
        steps = int(seconds * hz)
        for _ in range(steps):
            self.now += 1.0 / hz
            self.s.tick()
        if hz == 0:
            self.now += seconds
        self.s.end_window()

    def test_a_healthy_window_is_remembered_between_windows(self):
        self.window(100.0)
        self.now += 3.0                                   # not listening for 3 s
        rate, age, ago = self.s.read_sampled()
        self.assertAlmostEqual(rate, 100.0, delta=2)
        self.assertLess(age, 0.05)                        # age is as of the end of the window
        self.assertAlmostEqual(ago, 3.0, delta=0.01)      # and we say how old that measurement is

    def test_a_silent_window_turns_the_topic_stale(self):
        self.window(100.0)
        self.window(0.0)                                  # the publisher stopped
        rate, age, ago = self.s.read_sampled()
        self.assertEqual((rate, age), (0.0, None))        # classify_topic: age None -> stale

    def test_start_of_a_window_does_not_flicker_to_stale(self):
        self.window(100.0)
        self.now += 3.5
        self.s.start_window()                             # new window, no message yet
        self.assertAlmostEqual(self.s.read_sampled()[0], 100.0, delta=2)   # still the last result
        self.now += 0.01
        self.s.tick()
        self.assertAlmostEqual(self.s.read_sampled()[0], 100.0, delta=2)   # one message is not enough

    def test_a_slower_rate_is_noticed_in_the_next_window(self):
        self.window(100.0)
        self.now += 3.5
        self.window(20.0)
        self.assertAlmostEqual(self.s.read_sampled()[0], 20.0, delta=2)

    def test_unsampled_topics_keep_the_continuous_reading(self):
        c = _TopicStats(2.0)
        for _ in range(100):
            self.now += 0.01
            c.tick()
        rate, age = c.read()
        self.assertAlmostEqual(rate, 100.0, delta=2)


if __name__ == "__main__":
    unittest.main()


def imu_cdr(sec, nsec=0, little=True):
    """Start of a serialized sensor_msgs/Imu: CDR encapsulation, then header.stamp."""
    import struct
    order = "<" if little else ">"
    return bytes([0, 1 if little else 0, 0, 0]) + struct.pack(order + "iI", sec, nsec) + b"\x08\x00\x00\x00imu_link"


class StampTest(unittest.TestCase):
    """Seen on the device indoors: the OpenRTK330 had no GNSS time and stamped every IMU message
    1980-01-05 23:59:42; AIO NAV started, its output stopped, and automatic start gave up."""

    def setUp(self):
        self.now = 1000.0
        patch = mock.patch.object(ros2.time, "monotonic", lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.s = _TopicStats(2.0, stamped=True)

    def feed(self, stamps, hz=100.0):
        for st in stamps:
            self.now += 1.0 / hz
            self.s.tick(imu_cdr(int(st), int(round((st % 1) * 1e9))))

    def test_reads_the_header_stamp_in_both_byte_orders(self):
        self.assertAlmostEqual(ros2.header_stamp(imu_cdr(1792481513, 500_000_000)), 1792481513.5)
        self.assertAlmostEqual(ros2.header_stamp(imu_cdr(1792481513, 0, little=False)), 1792481513.0)
        self.assertIsNone(ros2.header_stamp(b"\x00\x01"))
        self.assertIsNone(ros2.header_stamp(None))

    def test_advancing_real_time_is_ok(self):
        self.feed([1792481513 + i / 100 for i in range(50)])
        self.assertEqual(self.s.time_status(), (True, ""))

    def test_receiver_epoch_without_gnss_time_is_not_ok(self):
        self.feed([315964782] * 5)
        ok, why = self.s.time_status()
        self.assertIs(ok, False)
        self.assertIn("1980-01-05 23:59:42", why)

    def test_a_frozen_real_stamp_is_not_ok_after_a_second(self):
        self.feed([1792481513.0] * 50)
        self.assertIsNone(self.s.time_status()[0])          # half a second: not known yet
        self.feed([1792481513.0] * 60)
        self.assertEqual(self.s.time_status(), (False, "time stamps do not advance"))
        self.feed([1792481514.0, 1792481514.01])            # time comes back
        self.assertEqual(self.s.time_status(), (True, ""))

    def test_kept_across_sampling_windows(self):
        self.s.start_window()
        self.feed([1792481513 + i / 100 for i in range(150)])
        self.s.end_window()
        self.now += 3.5
        self.assertEqual(self.s.time_status(), (True, ""))
        self.s.start_window()                               # next window: the stamp froze meanwhile
        self.feed([1792481514.49] * 150)
        self.assertIs(self.s.time_status()[0], False)

    def test_untyped_topics_are_not_judged(self):
        s = _TopicStats(2.0)
        s.tick(imu_cdr(315964782))
        self.assertEqual(s.time_status(), (None, ""))


def imu_full(gyro=(0.0, 0.0, 0.001), acc=(0.05, 0.1, -9.75), frame=b"imu_link", sec=1792481513):
    """A whole serialized sensor_msgs/Imu (little-endian CDR), as the driver publishes it."""
    import struct
    body = struct.pack("<iI", sec, 0) + struct.pack("<I", len(frame) + 1) + frame + b"\x00"
    body += b"\x00" * ((-len(body)) % 8)
    body += struct.pack("<4d", 0, 0, 0, 1) + struct.pack("<9d", *([0.0] * 9))
    body += struct.pack("<3d", *gyro) + struct.pack("<9d", *([0.0] * 9))
    body += struct.pack("<3d", *acc) + struct.pack("<9d", *([0.0] * 9))
    return bytes([0, 1, 0, 0]) + body


class MotionTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        patch = mock.patch.object(ros2.time, "monotonic", lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)

    def test_reads_gyro_and_acceleration(self):
        g, a = ros2.imu_motion(imu_full(gyro=(0.3, 0.4, 0.0), acc=(0.0, 6.0, 8.0)))
        self.assertAlmostEqual(g, 0.5)
        self.assertAlmostEqual(a, 10.0)
        g, _ = ros2.imu_motion(imu_full(gyro=(0.3, 0.4, 0.0), frame=b"a_much_longer_frame_id"))
        self.assertAlmostEqual(g, 0.5)                    # padding after the frame id
        self.assertIsNone(ros2.imu_motion(b"\x00\x01\x00\x00short"))

    def test_motion_summary_of_the_last_window_is_kept_between_windows(self):
        s = _TopicStats(2.0, stamped=True, imu=True)
        s.start_window()
        for i in range(150):
            self.now += 0.01
            s.tick(imu_full(gyro=(0.0, 0.0, 0.3 if i % 2 else 0.1), acc=(0.0, 0.0, 9.8 + (i % 2))))
        s.end_window()
        self.now += 3.0
        m = s.motion_status()
        self.assertAlmostEqual(m["gyro_max"], 0.3)
        self.assertAlmostEqual(m["accel_std"], 0.5, places=2)
        self.now += 10.0
        self.assertIsNone(s.motion_status())             # too old: unknown, not "still"


class ClockFeedTest(unittest.TestCase):
    def test_imu_stamps_reach_the_gnss_clock_feeder(self):
        got = []
        feeder = type("F", (), {"add": lambda self, wall, stamp: got.append((wall, stamp))})()
        s = _TopicStats(2.0, stamped=True, imu=True, clock_feeder=feeder)
        s.tick(imu_full(sec=1792481513))
        s.tick(b"\x00\x01")                                   # unreadable: not passed on
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], 1792481513.0)
