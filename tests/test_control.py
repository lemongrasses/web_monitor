import copy
import os
import stat
import tempfile
import unittest

from aio_system_dashboard.actions import process_control
from aio_system_dashboard.actions.registry import ActionError, ActionRegistry
from aio_system_dashboard.config import DEFAULTS, Config


class FakeEvents:
    def add(self, *a, **k):
        pass


def make(group=("aio_nav", "dso"), aio_nav_path=""):
    data = copy.deepcopy(DEFAULTS)
    data["fake"]["enabled"] = True
    data["nav"]["aio_nav_config"] = aio_nav_path
    data["nav"]["control_group"] = list(group)
    data["services"] = {
        "aio_nav": {"label": "AIO NAV", "process_pattern": "x/aio_nav_node", "launch": "aio-nav"},
        "dso": {"label": "DSO", "process_pattern": "x/dso_live", "launch": "aio-nav-dso"},
        "camera": {"label": "Camera", "unit": "camera.service", "restartable": True},
    }
    return Config(data, None)


class FakeFake:
    def __init__(self):
        self.states = {}

    def set_service(self, key, state):
        self.states[key] = state


class ControlActionsTest(unittest.TestCase):
    def test_targets_cover_each_process_and_the_group(self):
        reg = ActionRegistry(make(), {}, FakeEvents(), lambda d: {})
        self.assertEqual(set(reg.actions["start_process"].targets), {"aio_nav", "dso", "nav_core"})
        self.assertEqual(set(reg.actions["restart_service"].targets), {"camera"})
        self.assertTrue(reg.actions["stop_process"].confirm)
        self.assertFalse(reg.actions["start_process"].confirm)

    def test_unlisted_target_rejected(self):
        reg = ActionRegistry(make(), {}, FakeEvents(), lambda d: {})
        with self.assertRaises(ActionError):
            reg.run("stop_process", "camera")

    def test_fake_group_controls_members_separately_or_together(self):
        cfg, fake = make(), FakeFake()
        process_control.control(cfg, "dso", "stop", 0, fake)
        self.assertEqual(fake.states, {"dso": "stopped"})
        process_control.control(cfg, "nav_core", "start", 0, fake)
        self.assertEqual(fake.states, {"dso": "running", "aio_nav": "running"})

    def test_group_members_follow_config_order(self):
        self.assertEqual(process_control.members(make(group=("dso", "aio_nav"))), ["dso", "aio_nav"])


class LauncherTest(unittest.TestCase):
    def test_launcher_is_found_from_install_folder_only(self):
        with tempfile.TemporaryDirectory() as root:
            cfgdir = os.path.join(root, "install", "aio_nav_ros", "share", "aio_nav_ros", "config")
            libdir = os.path.join(root, "install", "aio_nav_ros", "lib", "aio_nav_ros")
            os.makedirs(cfgdir)
            os.makedirs(libdir)
            yml = os.path.join(cfgdir, "aio_nav.yaml")
            with open(yml, "w") as f:
                f.write("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n")
            wrapper = os.path.join(libdir, "aio-nav")
            with open(wrapper, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IXUSR)
            cfg = make(aio_nav_path=yml)
            self.assertEqual(cfg.launcher("aio-nav"), wrapper)
            self.assertIsNone(cfg.launcher("aio-nav-dso"))   # not installed -> reported, not guessed


class StartStabilityTest(unittest.TestCase):
    def _cfg(self, root, body):
        cfgdir = os.path.join(root, "install", "aio_nav_ros", "share", "aio_nav_ros", "config")
        libdir = os.path.join(root, "install", "aio_nav_ros", "lib", "aio_nav_ros")
        os.makedirs(cfgdir)
        os.makedirs(libdir)
        yml = os.path.join(cfgdir, "aio_nav.yaml")
        with open(yml, "w") as f:
            f.write("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n")
        wrapper = os.path.join(libdir, "aio-nav")
        with open(wrapper, "w") as f:
            f.write("#!/bin/bash\n" + body + "\n")
        os.chmod(wrapper, 0o755)
        cfg = make(aio_nav_path=yml)
        cfg.data["fake"]["enabled"] = False
        cfg.data["services"]["aio_nav"]["process_pattern"] = "^[^ ]*zzstab[l]e"
        return cfg

    def test_program_that_dies_right_after_launch_is_a_failure(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self._cfg(root, 'exec -a "zzstab""le" sleep 1')
            r = process_control.start_one(cfg, "aio_nav", wait_s=3, stable_s=2)
            self.assertFalse(r["ok"])
            self.assertIn("exited right after starting", r["text"])

    def test_program_that_stays_up_is_started(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self._cfg(root, 'exec -a "zzstab""le" sleep 30')
            try:
                r = process_control.start_one(cfg, "aio_nav", wait_s=3, stable_s=1)
                self.assertTrue(r["ok"], r)
                # a second start must not launch a duplicate
                self.assertIn("already running", process_control.start_one(cfg, "aio_nav")["text"])
            finally:
                process_control.stop_many(cfg, ["aio_nav"], 2)


if __name__ == "__main__":
    unittest.main()
