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
