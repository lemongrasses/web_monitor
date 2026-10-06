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


if __name__ == "__main__":
    unittest.main()
