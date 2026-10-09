import copy
import unittest

from aio_system_dashboard.collectors.gnss_monitor import GnssMonitor, classify, sigma_h
from aio_system_dashboard.config import DEFAULTS, Config


class Store:
    def __init__(self):
        self.d = {}

    def set(self, k, v):
        self.d[k] = v


class Clock:
    t = 10.0

    def __call__(self):
        return self.t


def monitor():
    data = copy.deepcopy(DEFAULTS)
    data["nav"]["aio_nav_config"] = ""
    clock, store = Clock(), Store()
    return GnssMonitor(Config(data, None), store, clock), clock, store


class ClassifyTest(unittest.TestCase):
    def test_status_maps_to_quality(self):
        self.assertEqual([classify(s) for s in (2, 1, 0, -1, None, 7)],
                         ["fixed", "float", "spp", "none", "none", "none"])

    def test_sigma_from_covariance(self):
        self.assertAlmostEqual(sigma_h([0.0009, 0, 0, 0, 0.0004, 0, 0, 0, 1.0]), 0.03)
        self.assertIsNone(sigma_h([float("nan")] * 9))
        self.assertIsNone(sigma_h(None))


class MonitorTest(unittest.TestCase):
    def test_each_stage(self):
        m, clock, _ = monitor()
        for status, quality in ((2, "fixed"), (1, "float"), (0, "spp"), (-1, "none")):
            m.on_fix(status, [0.01] * 9)
            self.assertEqual(m.snapshot()["quality"], quality)

    def test_no_message_is_no_signal_and_so_is_a_silent_receiver(self):
        m, clock, _ = monitor()
        self.assertEqual(m.snapshot()["quality"], "none")
        m.on_fix(2, [0.0009] * 9)
        self.assertEqual(m.snapshot()["quality"], "fixed")
        clock.t += 3.5                              # timeout_s is 3
        snap = m.snapshot()
        self.assertEqual((snap["quality"], snap["label"]), ("none", "No signal"))
        self.assertIsNone(snap["sigma_h_m"])


class DriverStatusTest(unittest.TestCase):
    """The OpenRTK330 driver sends status -1 with a valid position (single epochs inside RTK float
    periods, 9-14 cm). That is a fix of unrated type, not 'no signal'."""

    BAG_MINUS1 = dict(status=-1, cov=[0.0077, 0, 0, 0, 0.0107, 0, 0, 0, 0.1115], cov_type=2,
                      lat=23.8692924, lon=121.5311)            # a real message from the bag

    def test_minus_one_with_a_position_is_an_unrated_fix(self):
        m, _, _ = monitor()
        m.on_fix(**self.BAG_MINUS1)
        snap = m.snapshot()
        self.assertEqual((snap["quality"], snap["label"]), ("unrated", "Fix (type unknown)"))
        self.assertAlmostEqual(snap["sigma_h_m"], 0.1034, places=3)

    def test_poor_unrated_fix(self):
        m, _, _ = monitor()
        m.on_fix(-1, [25.0, 0, 0, 0, 25.0, 0, 0, 0, 50.0], 2, 23.8, 121.5)
        self.assertEqual(m.snapshot()["quality"], "unrated_poor")

    def test_no_usable_position_is_still_no_signal(self):
        m, _, _ = monitor()
        for cov, cov_type, lat, lon in (([0.01] * 9, 2, 0.0, 0.0),            # 0/0
                                        ([0.01] * 9, 0, 23.8, 121.5),         # covariance unknown
                                        ([0.01] * 9, 2, float("nan"), 121.5),
                                        ([0.0] * 9, 2, 23.8, 121.5)):        # no accuracy
            m.on_fix(-1, cov, cov_type, lat, lon)
            snap = m.snapshot()
            self.assertEqual(snap["quality"], "none", (cov_type, lat, lon))
            self.assertIn("reports no position", snap["reason"])

    def test_rated_statuses_unchanged(self):
        m, _, _ = monitor()
        for status, quality in ((2, "fixed"), (1, "float"), (0, "spp")):
            m.on_fix(status, [0.0009, 0, 0, 0, 0.0009, 0, 0, 0, 0.01], 2, 23.8, 121.5)
            self.assertEqual(m.snapshot()["quality"], quality)

    def test_reasons_tell_silence_from_no_fix(self):
        m, clock, _ = monitor()
        self.assertIn("no GNSS message received yet", m.snapshot()["reason"])
        m.on_fix(**self.BAG_MINUS1)
        self.assertEqual(m.snapshot()["reason"], "")
        clock.t += 16.8 * 3600
        self.assertIn("no GNSS message for 16.8 h", m.snapshot()["reason"])


class LampTest(unittest.TestCase):
    def test_levels_and_advisory(self):
        from aio_system_dashboard.state.health import GNSS_LEVEL, HealthEngine
        from aio_system_dashboard.state.indicator import FAULT, WARNING
        self.assertEqual(GNSS_LEVEL["unrated"][0], WARNING)
        self.assertEqual(GNSS_LEVEL["unrated_poor"][0], FAULT)
        self.assertEqual(HealthEngine._gnss_detail({"available": True, "quality": "none",
                                                     "reason": "no GNSS message for 3 h"}),
                         "no GNSS message for 3 h")
        self.assertEqual(HealthEngine._gnss_detail({"available": True, "quality": "unrated",
                                                     "sigma_h_m": 0.1}), "horizontal accuracy 10 cm")


if __name__ == "__main__":
    unittest.main()
