import copy
import tempfile
import unittest
from pathlib import Path

from aio_system_dashboard.collectors.ros_guard import RosIsolationGuard, same_ros_setup
from aio_system_dashboard.config import DEFAULTS, Config


class Events:
    def __init__(self):
        self.items = []

    def add(self, *a, **k):
        self.items.append(a)


class Rig:
    def __init__(self, test, pids=(100,), same=True, enabled=True, **over):
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["ros"].update(isolation_restart=enabled, **over)
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.t, self.wall, self.restarts, self.events = 1000.0, 5_000_000.0, 0, Events()
        self.pids, self.same = list(pids), same
        self.state = Path(tmp.name) / "s.json"
        self.guard = RosIsolationGuard(Config(data, None), self.events, lambda: self.pids, self._restart,
                                       self.state, lambda: self.t, lambda: self.wall, lambda p: self.same)

    def _restart(self):
        self.restarts += 1

    def run(self, seconds, foreign=0, step=2.0):
        for _ in range(int(seconds / step)):
            self.t += step
            self.wall += step
            self.guard.update(foreign)


class GuardTest(unittest.TestCase):
    def test_restarts_when_drivers_run_in_our_domain_but_no_node_is_visible(self):
        r = Rig(self)
        r.run(120)
        self.assertEqual(r.restarts, 1)
        self.assertTrue(any("stuck" in e[2] for e in r.events.items))

    def test_not_when_other_nodes_are_visible(self):
        r = Rig(self)
        r.run(300, foreign=2)
        self.assertEqual(r.restarts, 0)

    def test_not_when_the_drivers_are_off(self):
        r = Rig(self, pids=())
        r.run(300)
        self.assertEqual(r.restarts, 0)

    def test_not_when_the_drivers_are_in_another_domain(self):
        r = Rig(self, same=False)
        r.run(300)
        self.assertEqual(r.restarts, 0)

    def test_waits_for_the_grace_time_and_a_minimum_uptime(self):
        r = Rig(self, isolation_grace_s=30)
        r.run(40)                                   # grace over, but the dashboard is under a minute old
        self.assertEqual(r.restarts, 0)
        r.run(30)
        self.assertEqual(r.restarts, 1)

    def test_never_loops(self):
        r = Rig(self, isolation_cooldown_s=600)
        r.run(120)
        self.assertEqual(r.restarts, 1)
        r.run(500)                                  # a restart that did not help
        self.assertEqual(r.restarts, 1)
        r.run(200)                                  # cooldown over
        self.assertEqual(r.restarts, 2)

    def test_a_node_appearing_resets_the_timer(self):
        r = Rig(self)
        r.run(50)                                   # 50 s isolated (grace is 30 s, but the dashboard is young)
        r.run(2, foreign=1)                         # a node shows up: the timer starts over
        r.run(20)                                   # isolated again, but not for 30 s yet
        self.assertEqual(r.restarts, 0)
        r.run(20)                                   # now 40 s in a row
        self.assertEqual(r.restarts, 1)

    def test_disabled(self):
        r = Rig(self, enabled=False)
        r.run(300)
        self.assertEqual(r.restarts, 0)


class SameSetupTest(unittest.TestCase):
    def test_compares_domain_and_localhost_of_a_real_process(self):
        import os
        me = os.getpid()
        self.assertTrue(same_ros_setup(me))                              # same process, same environment
        self.assertFalse(same_ros_setup(me, {"ROS_DOMAIN_ID": "999", "ROS_LOCALHOST_ONLY": "0"}))
        self.assertFalse(same_ros_setup(2 ** 22 + 12345))                # no such process


if __name__ == "__main__":
    unittest.main()
