import copy
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from pathlib import Path

from aio_system_dashboard.actions import process_control
from aio_system_dashboard.actions.registry import ActionError, ActionRegistry
from aio_system_dashboard.config import DEFAULTS, Config


def _isolate_state(test):
    """process_control records 'should be running' in state/; keep tests out of the real one."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    patch = mock.patch.object(Config, "state_file", lambda _self, name: Path(tmp.name) / name)
    patch.start()
    test.addCleanup(patch.stop)


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
    def setUp(self):
        _isolate_state(self)

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
    def setUp(self):
        _isolate_state(self)

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



class WorkDirTest(unittest.TestCase):
    def setUp(self):
        _isolate_state(self)

    def test_programs_run_next_to_the_output_folder(self):
        with tempfile.TemporaryDirectory() as root:
            cfgdir = os.path.join(root, "install", "aio_nav_ros", "share", "aio_nav_ros", "config")
            os.makedirs(cfgdir)
            os.makedirs(os.path.join(root, "output"))
            yml = os.path.join(cfgdir, "aio_nav.yaml")
            with open(yml, "w") as f:
                f.write("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n")
            self.assertEqual(os.path.realpath(process_control.work_dir(make(aio_nav_path=yml))),
                             os.path.realpath(root))



class TailTest(unittest.TestCase):
    def test_error_lines_come_first(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.log"
            f.write_text("[INFO] up\n[INFO] topics ready\nprog: Assertion `a' failed.\n[ros2run]: Aborted\n[INFO] bye\n")
            self.assertEqual(process_control._tail(f), "prog: Assertion `a' failed. | [ros2run]: Aborted")
            f.write_text("one\ntwo\nthree\nfour\n")
            self.assertEqual(process_control._tail(f), "two | three | four")



class UserResultTest(unittest.TestCase):
    """The Overview page must not mention DSO, whatever DSO did."""

    def setUp(self):
        _isolate_state(self)
        self.cfg = make()

    def _result(self, nav_ok, dso_ok, dso_text="DSO exited right after starting: Assertion failed (log: /x/dso.log)"):
        return {"success": nav_ok and dso_ok, "summary": "AIO NAV started; " + dso_text,
                "parts": [{"key": "aio_nav", "ok": nav_ok, "text": "AIO NAV started" if nav_ok else
                           "AIO NAV did not start: bind error (log: /x/aio_nav.log)"},
                          {"key": "dso", "ok": dso_ok, "text": dso_text}]}

    def test_a_failed_dso_does_not_make_start_fail_or_show_up(self):
        r = process_control.user_result(self.cfg, "start", self._result(True, False))
        self.assertEqual(r, {"success": True, "summary": "AIO NAV started"})

    def test_a_failed_filter_is_reported_without_server_paths(self):
        r = process_control.user_result(self.cfg, "start", self._result(False, True, "DSO started"))
        self.assertFalse(r["success"])
        self.assertEqual(r["summary"], "AIO NAV did not start: bind error")
        self.assertNotIn("dso", str(r).lower())

    def test_stop_and_already_running(self):
        r = process_control.user_result(self.cfg, "stop", {"success": True, "summary": "stopped 2 process(es)"})
        self.assertEqual(r, {"success": True, "summary": "AIO NAV stopped"})
        res = {"success": True, "parts": [{"key": "aio_nav", "ok": True, "text": "AIO NAV already running"},
                                          {"key": "dso", "ok": True, "text": "DSO already running"}]}
        self.assertEqual(process_control.user_result(self.cfg, "start", res)["summary"], "AIO NAV already running")



class UserUnitTest(unittest.TestCase):
    def setUp(self):
        _isolate_state(self)
        cfg = make()
        cfg.data["services"]["drivers"] = {"label": "Sensor drivers", "user_unit": "drv.service",
                                           "optional": True}
        cfg.data["fake"]["enabled"] = False
        self.cfg = cfg

    def _run(self, returncode=0, stderr="", active="active"):
        from aio_system_dashboard.actions import service_control
        done = types_ns(returncode=returncode, stdout="", stderr=stderr)
        for patch in (mock.patch.object(service_control.subprocess, "run", return_value=done),
                      mock.patch.object(service_control, "systemd_unit_state",
                                        return_value={"active": active, "sub": "x"})):
            m = patch.start()
            self.addCleanup(patch.stop)
            if "run" in str(patch.attribute):
                run = m
        return service_control, run

    def test_runs_systemctl_user_with_a_bus_environment(self):
        sc, run = self._run(active="active")
        r = sc.control_user_service(self.cfg, "drivers", "start", 30)
        self.assertTrue(r["success"])
        args, kw = run.call_args
        self.assertEqual(args[0], ["systemctl", "--user", "start", "drv.service"])
        self.assertIn("XDG_RUNTIME_DIR", kw["env"])
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", kw["env"])

    def test_stop_needs_the_unit_to_be_inactive(self):
        sc, _ = self._run(active="active")
        self.assertFalse(sc.control_user_service(self.cfg, "drivers", "stop", 30)["success"])
        sc, _ = self._run(active="inactive")
        self.assertTrue(sc.control_user_service(self.cfg, "drivers", "stop", 30)["success"])

    def test_systemctl_error_is_reported(self):
        sc, _ = self._run(returncode=1, stderr="Unit drv.service not found.\n")
        r = sc.control_user_service(self.cfg, "drivers", "start", 30)
        self.assertEqual((r["success"], r["summary"]), (False, "Unit drv.service not found."))

    def test_only_listed_verbs(self):
        sc, run = self._run()
        self.assertFalse(sc.control_user_service(self.cfg, "drivers", "disable", 30)["success"])
        run.assert_not_called()

    def test_actions_target_only_user_unit_services(self):
        reg = ActionRegistry(self.cfg, {}, FakeEvents(), lambda d: {})
        for a in ("start_driver", "stop_driver", "restart_driver"):
            self.assertEqual(set(reg.actions[a].targets), {"drivers"})
        self.assertTrue(reg.actions["stop_driver"].confirm and not reg.actions["start_driver"].confirm)
        with self.assertRaises(ActionError):
            reg.run("stop_driver", "camera")


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


if __name__ == "__main__":
    unittest.main()
