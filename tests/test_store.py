"""The event log (state/store.py)."""

import unittest

from aio_system_dashboard.state.store import EventLog


class EventTimeTest(unittest.TestCase):
    def test_events_before_the_clock_is_set_get_their_real_time(self):
        """Seen 2026-10-21: the Jetson starts each power-on at 1970, and the Maintenance page listed
        the first minute's events at "08:00:40" even after GNSS time had set the clock."""
        wall, mono = [40.0], [30.0]
        log = EventLog(None, clock=lambda: wall[0], mono=lambda: mono[0])
        log.add("info", "system", "Dashboard started")
        self.assertEqual(log.recent()[0]["ts"], 40.0)               # nothing better known yet
        mono[0] += 50.0
        wall[0] = 1792570000.0                                       # GNSS time set the clock
        log.add("info", "nav", "AIO NAV running")
        started, running = reversed(log.recent())
        self.assertEqual(running["ts"], 1792570000.0)
        self.assertAlmostEqual(started["ts"], 1792570000.0 - 50.0)   # 50 s earlier, on the monotonic clock


if __name__ == "__main__":
    unittest.main()
