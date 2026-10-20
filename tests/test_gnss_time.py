"""GNSS time samples for chrony (collectors/gnss_time.py)."""

import ctypes
import unittest

from aio_system_dashboard.collectors import gnss_time as gt


class LayoutTest(unittest.TestCase):
    def test_struct_matches_chrony_on_64_bit(self):
        # chrony refclock_shm.c / gpsd ntpshm.h on LP64: 96 bytes, the fields at these offsets
        self.assertEqual(ctypes.sizeof(gt.shmTime), 96)
        off = {name: getattr(gt.shmTime, name).offset for name, _ in gt.shmTime._fields_}
        self.assertEqual((off["clockTimeStampSec"], off["receiveTimeStampSec"], off["valid"],
                          off["clockTimeStampNSec"], off["receiveTimeStampNSec"]), (8, 24, 48, 52, 56))

    def test_put_sample_mode_1(self):
        seg = gt.shmTime()
        gt.put_sample(seg, 1792482148.0125, 1792482148.25)
        self.assertEqual((seg.mode, seg.valid, seg.count % 2), (1, 1, 0))
        self.assertEqual(seg.clockTimeStampSec, 1792482148)
        self.assertAlmostEqual(seg.clockTimeStampNSec, 12_500_000, delta=1000)    # float epoch: < 1 us
        self.assertEqual((seg.receiveTimeStampSec, seg.receiveTimeStampUSec), (1792482148, 250_000))
        gt.put_sample(seg, 1792482149.9999999999, 1792482150.0)
        self.assertEqual((seg.clockTimeStampSec, seg.clockTimeStampNSec), (1792482150, 0))
        self.assertEqual(seg.count, 4)


class WindowTest(unittest.TestCase):
    def window(self, delays, t0=1792482148.0, hz=100):
        w = gt.OffsetWindow(latency_s=0.0056)
        for i, d in enumerate(delays):
            s = t0 + i / hz
            w.add(s + d, s)
        return w

    def test_the_fastest_message_gives_the_sample(self):
        # seen on the device: a stall, then a burst of messages 1.2 s late (the median was 1.2 s off)
        delays = [1.24] * 40 + [0.012] * 5 + [0.0057] + [0.009] * 4
        (clock, recv), why, ds = self.window(delays).sample()
        self.assertEqual(why, "")
        self.assertAlmostEqual(clock - recv, 0.0056 - 0.0057, places=6)    # ~0: the offset is right
        self.assertEqual(len(ds), 50)

    def test_refused_without_time_or_messages(self):
        self.assertEqual(self.window([0.01] * 10).sample()[1], "10 IMU messages")
        self.assertEqual(self.window([0.01] * 50, t0=315964782.0).sample()[1], "no GNSS time yet")
        w = gt.OffsetWindow(latency_s=0.0056)
        for i in range(50):
            w.add(1792482148.0 + i / 100, 1792482148.0)            # stamp frozen
        self.assertEqual(w.sample()[1], "time stamps do not advance")

    def test_window_is_emptied(self):
        w = self.window([0.01] * 50)
        w.sample()
        self.assertEqual(w.sample()[1], "0 IMU messages")


class FakeShm:
    def __init__(self):
        self.seg, self.error, self.samples = object(), "", []

    def put(self, clock_t, receive_t):
        self.samples.append(clock_t - receive_t)
        return True


class FeederTest(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.shm = FakeShm()
        self.f = gt.GnssClockFeeder(0.0056, self.shm, clock=lambda: self.t[0])
        self.wall = 1792482148.0

    def windows(self, n, clock_error_s=0.0, delay=0.0057):
        """n windows of 0.5 s at 100 Hz; the system clock is clock_error_s ahead of GNSS time."""
        for _ in range(n):
            for _ in range(50):
                self.t[0] += 0.01
                self.wall += 0.01
                self.f.add(self.wall + clock_error_s + delay, self.wall)
        self.t[0] += 0.01
        self.f.add(self.wall + clock_error_s + delay, self.wall)        # closes the last window

    def test_warm_up_then_samples(self):
        self.windows(2)
        self.assertEqual(self.shm.samples, [])                # 2 samples: nothing sent yet
        self.windows(3)
        self.assertGreaterEqual(len(self.shm.samples), 2)
        self.assertAlmostEqual(self.shm.samples[-1], -0.0001, places=4)
        self.assertTrue(self.f.status()["chrony"])

    def test_a_lone_outlier_is_held_back(self):
        self.windows(6)
        n = len(self.shm.samples)
        self.windows(2, delay=0.5)                            # every message late: offset -0.5 s
        self.assertEqual(len(self.shm.samples), n)            # nothing of it reached chrony
        self.assertIn("held back", self.f.status()["reason"])
        self.windows(2)
        self.assertGreater(len(self.shm.samples), n)

    def test_a_real_clock_step_is_followed(self):
        self.windows(6)
        self.windows(5, clock_error_s=-30.0)                  # chrony stepped the system clock
        self.assertAlmostEqual(self.shm.samples[-1], 30.0, places=2)
        self.assertAlmostEqual(self.f.status()["offset_s"], 30.0, places=2)

    def test_without_chrony_nothing_breaks(self):
        f = gt.GnssClockFeeder(0.0056, None, clock=lambda: self.t[0])
        for _ in range(300):
            self.t[0] += 0.01
            self.wall += 0.01
            f.add(self.wall + 0.0057, self.wall)
        st = f.status()
        self.assertFalse(st["chrony"])
        self.assertIsNotNone(st["offset_s"])


if __name__ == "__main__":
    unittest.main()
