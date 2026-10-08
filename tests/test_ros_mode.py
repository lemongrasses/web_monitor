import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_dashboard_replay import mode as ros_mode
from aio_system_dashboard.config import DEFAULTS, Config

LIVE = "aio_nav_node:\n  ros__parameters:\n    ros_domain_id: 10\n    ros_localhost_only: 1\n    use_sim_time: false\n    output_rate: 100.0\n"
BAG = "aio_nav_node:\n  ros__parameters:\n    ros_domain_id: 13\n    ros_localhost_only: 0\n    use_sim_time: true\n    output_rate: 100.0\n"


class Events:
    def __init__(self):
        self.items = []

    def add(self, *a, **k):
        self.items.append(a)


class RosModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfgdir = Path(self.tmp.name) / "install" / "aio_nav_ros" / "share" / "aio_nav_ros" / "config"
        cfgdir.mkdir(parents=True)
        (cfgdir / "aio_nav.yaml").write_text(LIVE)
        (cfgdir / "aio_nav_bag.yaml").write_text(BAG)
        self.cfgdir = cfgdir
        self.state = Path(self.tmp.name) / "state" / "ros_mode.json"
        patch = mock.patch.object(Config, "ros_mode_file", lambda _self: self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def make(self, **ros):
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = str(self.cfgdir / "aio_nav.yaml")
        data["ros"].update({"mode": "live", **ros})
        return Config(data, None)

    def test_live_uses_aio_nav_yaml_and_its_domain(self):
        cfg = self.make()
        self.assertEqual(cfg.mode_config_path(), str(self.cfgdir / "aio_nav.yaml"))
        self.assertEqual(cfg.ros_env(), {"ROS_DOMAIN_ID": "10", "ROS_LOCALHOST_ONLY": "1"})

    def test_bag_mode_reads_the_bag_file_after_a_restart(self):
        cfg = self.make()
        cfg.save_ros_mode("bag")
        again = self.make()                       # what the restarted dashboard sees
        self.assertEqual(again.ros_mode(), "bag")
        self.assertEqual(again.aio_nav["path"], str(self.cfgdir / "aio_nav_bag.yaml"))
        self.assertEqual(again.ros_env(), {"ROS_DOMAIN_ID": "13", "ROS_LOCALHOST_ONLY": "0"})
        self.assertTrue(again.aio_nav["use_sim_time"])

    def test_info_lists_every_mode_with_its_own_file(self):
        info = self.make().ros_mode_info()
        by = {m["name"]: m for m in info["modes"]}
        self.assertEqual((by["live"]["domain_id"], by["live"]["localhost_only"]), (10, True))
        self.assertEqual((by["bag"]["domain_id"], by["bag"]["localhost_only"], by["bag"]["config"]),
                         (13, False, "aio_nav_bag.yaml"))
        self.assertTrue(by["live"]["found"] and by["bag"]["found"])

    def test_missing_bag_file_is_flagged_not_guessed(self):
        (self.cfgdir / "aio_nav_bag.yaml").unlink()
        by = {m["name"]: m for m in self.make().ros_mode_info()["modes"]}
        self.assertFalse(by["bag"]["found"])
        self.assertIsNone(by["bag"]["domain_id"])

    def test_a_mode_can_still_override_the_file(self):
        cfg = self.make()
        cfg.data["ros"]["modes"]["live"]["domain_id"] = 7
        self.assertEqual(cfg.ros_env()["ROS_DOMAIN_ID"], "7")

    def test_no_mode_keeps_the_environment(self):
        cfg = self.make(mode=None)
        self.assertEqual(cfg.ros_env(), {})
        self.assertEqual(cfg.ros_mode_info()["label"], "Environment")

    def edit_live(self, **changes):
        text = LIVE
        for k, v in changes.items():
            text = text.replace(f"{k}: ", f"{k}: {v} #", 1)
        f = self.cfgdir / "aio_nav.yaml"
        f.write_text(text)
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # a later edit

    def test_edits_to_aio_nav_yaml_are_seen_without_a_restart(self):
        cfg = self.make()
        self.assertEqual(cfg.expected_nav_rate, 100.0)
        self.edit_live(output_rate=50.0)
        self.assertEqual(cfg.expected_nav_rate, 100.0)           # reused for a moment ...
        cfg._aio_nav_at -= cfg.AIO_NAV_MAX_AGE_S + 1
        self.assertEqual(cfg.expected_nav_rate, 50.0)            # ... then read again

    def test_start_reads_the_file_at_once(self):
        from aio_system_dashboard.actions import process_control
        cfg = self.make()
        self.assertEqual(cfg.ros_env()["ROS_DOMAIN_ID"], "10")
        self.edit_live(ros_domain_id=12)
        cfg.data["fake"]["enabled"] = True                       # no real process in a test
        with mock.patch.object(process_control.time, "sleep", lambda s: None):
            process_control.control(cfg, "aio_nav", "start", 0.1)
        self.assertEqual(cfg.ros_env()["ROS_DOMAIN_ID"], "12")   # AIO NAV starts with the new domain

    def test_garbage_state_file_falls_back_to_config(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text("{not json")
        cfg = self.make()
        self.assertEqual(cfg.ros_mode(), "live")
        self.state.write_text('{"mode": "evil"}')
        self.assertEqual(self.make().ros_mode(), "live")
        with self.assertRaises(ValueError):
            cfg.save_ros_mode("evil")

    def test_apply_stops_nav_saves_and_restarts(self):
        cfg, calls, ev = self.make(), [], Events()
        r = ros_mode.apply_mode(cfg, "bag", lambda: calls.append("stop") or {"success": True},
                                lambda: calls.append("restart"), ev)
        self.assertTrue(r["success"] and r["changed"] and r["restarting"])
        self.assertEqual(calls, ["stop", "restart"])
        self.assertEqual(cfg.ros_mode(), "bag")
        self.assertTrue(ev.items)

    def test_same_mode_is_a_no_op(self):
        calls = []
        r = ros_mode.apply_mode(self.make(), "live", lambda: calls.append("stop") or {"success": True},
                                lambda: calls.append("restart"))
        self.assertEqual((r["success"], r["changed"], calls), (True, False, []))

    def test_unknown_mode_rejected(self):
        r = ros_mode.apply_mode(self.make(), "13", lambda: {"success": True}, lambda: None)
        self.assertEqual((r["success"], r["status"]), (False, 400))

    def test_mode_without_its_file_cannot_be_applied(self):
        (self.cfgdir / "aio_nav_bag.yaml").unlink()
        calls = []
        r = ros_mode.apply_mode(self.make(), "bag", lambda: calls.append("stop") or {"success": True},
                                lambda: calls.append("restart"))
        self.assertFalse(r["success"])
        self.assertEqual(calls, [])

    def test_failed_stop_keeps_the_old_mode(self):
        cfg, calls = self.make(), []
        r = ros_mode.apply_mode(cfg, "bag", lambda: {"success": False, "summary": "stuck"},
                                lambda: calls.append("restart"))
        self.assertFalse(r["success"])
        self.assertEqual((cfg.ros_mode(), calls), ("live", []))


if __name__ == "__main__":
    unittest.main()


class FollowConfigAfterStartTest(unittest.TestCase):
    def hook(self, env_at_start, env_now, action="start_process", target="nav_core"):
        from types import SimpleNamespace
        try:
            from aio_system_dashboard.__main__ import DashboardContext
        except ImportError as e:                       # Flask not importable here
            self.skipTest(str(e))
        calls = []
        cfg = type("C", (), {"ros_env": lambda s: env_now,
                             "__getitem__": lambda s, k: {"nav": {"service": "aio_nav"}, "data": {"roots": []}}[k]})()
        ctx = SimpleNamespace(cfg=cfg, ros_env_at_start=env_at_start, data=None,
                              events=SimpleNamespace(add=lambda *a, **k: calls.append(("event", a[2]))),
                              request_restart=lambda: calls.append(("restart",)))
        DashboardContext._after_action(ctx, action, target, {"success": True})
        return calls, ctx

    def test_domain_change_restarts_the_dashboard(self):
        calls, _ = self.hook({"ROS_DOMAIN_ID": "10"}, {"ROS_DOMAIN_ID": "12"})
        self.assertIn(("restart",), calls)

    def test_same_domain_no_restart_but_data_roots_refreshed(self):
        calls, ctx = self.hook({"ROS_DOMAIN_ID": "10"}, {"ROS_DOMAIN_ID": "10"})
        self.assertEqual(calls, [])
        self.assertIsNotNone(ctx.data)

    def test_other_actions_are_ignored(self):
        calls, _ = self.hook({"ROS_DOMAIN_ID": "10"}, {"ROS_DOMAIN_ID": "12"}, action="stop_process")
        self.assertEqual(calls, [])
        calls, _ = self.hook({"ROS_DOMAIN_ID": "10"}, {"ROS_DOMAIN_ID": "12"}, target="dso")
        self.assertEqual(calls, [])
