import copy
import os
import tempfile
import unittest
from pathlib import Path

from aio_system_dashboard import config as config_mod
from aio_system_dashboard.actions import ros_mode
from aio_system_dashboard.config import DEFAULTS, Config


class Events:
    def __init__(self):
        self.items = []

    def add(self, *a, **k):
        self.items.append(a)


class RosModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["ros"]["mode"] = "live"
        self.cfg = Config(data, None)
        self.cfg.ros_mode_file = lambda: Path(self.tmp.name) / "state" / "ros_mode.json"

    def test_default_is_live_10_1(self):
        self.assertEqual(self.cfg.ros_env(), {"ROS_DOMAIN_ID": "10", "ROS_LOCALHOST_ONLY": "1"})

    def test_no_mode_keeps_the_environment(self):
        self.cfg.data["ros"]["mode"] = None
        self.assertEqual(self.cfg.ros_env(), {})
        self.assertEqual(self.cfg.ros_mode_info()["label"], "Environment")

    def test_bag_is_13_0_and_choice_survives_restart(self):
        self.cfg.save_ros_mode("bag")
        self.assertEqual(self.cfg.ros_env(), {"ROS_DOMAIN_ID": "13", "ROS_LOCALHOST_ONLY": "0"})
        self.assertEqual(self.cfg.ros_mode(), "bag")

    def test_garbage_state_file_falls_back_to_config(self):
        f = self.cfg.ros_mode_file()
        f.parent.mkdir(parents=True)
        f.write_text("{not json")
        self.assertEqual(self.cfg.ros_mode(), "live")
        f.write_text('{"mode": "evil"}')
        self.assertEqual(self.cfg.ros_mode(), "live")
        with self.assertRaises(ValueError):
            self.cfg.save_ros_mode("evil")

    def test_apply_stops_nav_saves_and_restarts(self):
        calls, ev = [], Events()
        r = ros_mode.apply_mode(self.cfg, "bag", lambda: calls.append("stop") or {"success": True},
                                lambda: calls.append("restart"), ev)
        self.assertTrue(r["success"] and r["changed"] and r["restarting"])
        self.assertEqual(calls, ["stop", "restart"])          # stopped before the restart
        self.assertEqual(self.cfg.ros_mode(), "bag")
        self.assertTrue(ev.items)

    def test_same_mode_is_a_no_op(self):
        calls = []
        r = ros_mode.apply_mode(self.cfg, "live", lambda: calls.append("stop") or {"success": True},
                                lambda: calls.append("restart"))
        self.assertEqual((r["success"], r["changed"], calls), (True, False, []))

    def test_unknown_mode_rejected(self):
        r = ros_mode.apply_mode(self.cfg, "13", lambda: {"success": True}, lambda: None)
        self.assertFalse(r["success"])
        self.assertEqual(r["status"], 400)

    def test_failed_stop_keeps_the_old_mode(self):
        calls = []
        r = ros_mode.apply_mode(self.cfg, "bag", lambda: {"success": False, "summary": "stuck"},
                                lambda: calls.append("restart"))
        self.assertFalse(r["success"])
        self.assertEqual((self.cfg.ros_mode(), calls), ("live", []))


if __name__ == "__main__":
    unittest.main()
