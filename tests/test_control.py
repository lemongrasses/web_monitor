import copy
import os
import stat
import tempfile
import time
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

    def test_starts_and_stops_never_overlap(self):
        """Found on the device: 20 Start/Stop presses at once let a Start and a Stop of AIO NAV run
        together; afterwards nothing ran but "should run" was set (the page showed a fault)."""
        import threading
        reg = ActionRegistry(make(), {}, FakeEvents(), lambda d: {})
        gate = threading.Event()
        for a in ("start_process", "stop_process", "restart_process"):
            reg.actions[a].run = lambda target: (gate.wait(5), {"success": True, "summary": ""})[1]
        t = threading.Thread(target=reg.run, args=("stop_process", "nav_core"))
        t.start()
        time.sleep(0.05)
        for action_id, target in (("start_process", "nav_core"), ("restart_process", "dso")):
            with self.assertRaises(ActionError) as e:
                reg.run(action_id, target)
            self.assertEqual(e.exception.status, 409)
        gate.set()
        t.join()
        self.assertTrue(reg.run("start_process", "nav_core")["success"])   # free again

    def test_start_request_age_survives_a_clock_step(self):
        """The wall clock steps when NTP or GNSS time arrives (an RTC 8 h ahead was seen): the age of
        a start request must not become hours, or negative ("starting" for ever)."""
        cfg = make()
        process_control.set_wanted(cfg, "aio_nav", True)
        real = time.time
        for step in (-8 * 3600, 8 * 3600):
            with mock.patch.object(process_control.time, "time", lambda: real() + step):
                self.assertLess(process_control.wanted_age_s(cfg, "aio_nav"), 5)
        with mock.patch.object(process_control, "_boot_id", lambda: "another-boot"):
            self.assertEqual(process_control.wanted_age_s(cfg, "aio_nav"), float("inf"))
        process_control.set_wanted(cfg, "aio_nav", False)
        self.assertIsNone(process_control.wanted_age_s(cfg, "aio_nav"))

    def test_a_program_still_exiting_after_a_stop_is_not_adopted(self):
        """Seen 2026-10-21: an operator Stop of AIO NAV + DSO; DSO (3 GB) took seconds to exit, its
        watchdog saw it running, marked it wanted again and started it 30 s later."""
        cfg = make()
        process_control.set_wanted(cfg, "dso", True)
        process_control.set_wanted(cfg, "dso", False)                 # the Stop
        self.assertFalse(process_control.adopt(cfg, "dso"))           # still exiting
        self.assertFalse(process_control.wanted(cfg, "dso"))
        later = time.monotonic() + process_control.ADOPT_AFTER_STOP_S + 1
        with mock.patch.object(process_control.time, "monotonic", lambda: later):
            self.assertTrue(process_control.adopt(cfg, "dso"))        # started by hand since
        self.assertTrue(process_control.wanted(cfg, "dso"))
        process_control.set_wanted(cfg, "dso", False)
        with mock.patch.object(process_control, "_boot_id", lambda: "another-boot"):
            self.assertTrue(process_control.adopt(cfg, "dso"))        # a stop in an earlier boot
        process_control.set_wanted(cfg, "aio_nav", True)              # a stop does not touch other
        process_control.set_wanted(cfg, "dso", False)                 # programs' start times
        self.assertLess(process_control.wanted_age_s(cfg, "aio_nav"), 5)

    def test_a_start_from_an_earlier_boot_does_not_count_for_this_boot(self):
        """Seen at a reboot (2026-10-21): DSO was wanted at power-off, and its watchdog started it on
        its own 30 s after the dashboard, ahead of the start-up coordinator's AIO NAV + DSO."""
        cfg = make()
        process_control.set_wanted(cfg, "dso", True)
        self.assertTrue(process_control.wanted(cfg, "dso", this_boot=True))
        with mock.patch.object(process_control, "_boot_id", lambda: "next-boot"):
            self.assertTrue(process_control.wanted(cfg, "dso"))                  # still on record ...
            self.assertFalse(process_control.wanted(cfg, "dso", this_boot=True))  # ... but from before
            process_control.set_wanted(cfg, "aio_nav", True)                     # AIO NAV started now:
            self.assertFalse(process_control.wanted(cfg, "dso", this_boot=True))  # DSO's start stays old
            self.assertEqual(process_control.wanted_age_s(cfg, "dso"), float("inf"))
            self.assertTrue(process_control.adopt(cfg, "dso"))                    # running now: this boot
            self.assertTrue(process_control.wanted(cfg, "dso", this_boot=True))
            self.assertLess(process_control.wanted_age_s(cfg, "dso"), 5)

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

    def test_restart_driver_of_a_user_unit_uses_systemctl_user_not_sudo(self):
        # The camera as its own point-lab unit: its Restart driver button must reach that unit.
        self.cfg.data["services"]["camera"] = {"label": "Camera", "user_unit": "cam.service",
                                               "restartable": True}
        reg = ActionRegistry(self.cfg, {}, FakeEvents(), lambda d: {})
        self.assertEqual(set(reg.actions["restart_service"].targets), {"camera"})
        sc, run = self._run(active="active")
        r = sc.restart_service(self.cfg, "camera", 30)
        self.assertTrue(r["success"])
        self.assertEqual(run.call_args[0][0], ["systemctl", "--user", "restart", "cam.service"])
        sc, _ = self._run(returncode=1, stderr="Unit cam.service not found.\n")
        self.assertEqual(sc.restart_service(self.cfg, "camera", 30)["summary"], "Unit cam.service not found.")


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)



class PgrepTest(unittest.TestCase):
    """pgrep() scans /proc itself (no child process) and must behave like `pgrep -f`."""

    def test_finds_a_real_process_by_command_line_and_not_a_mention_of_it(self):
        import subprocess
        from aio_system_dashboard.collectors import services
        p = subprocess.Popen(["bash", "-c", 'exec -a "zzpgr""ep" sleep 20'])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        time.sleep(0.3)
        self.assertEqual(services.pgrep("^[^ ]*zzpgre[p]", max_age_s=0), [p.pid])
        self.assertEqual(services.pgrep("^[^ ]*zzpgre[p]x", max_age_s=0), [])       # no match
        # a shell that merely mentions the name does not match the anchored pattern
        q = subprocess.Popen(["bash", "-c", "sleep 20 # zzpgrep"])
        self.addCleanup(q.wait)
        self.addCleanup(q.kill)
        time.sleep(0.3)
        self.assertNotIn(q.pid, services.pgrep("^[^ ]*zzpgre[p]", max_age_s=0))

    def test_cache_is_bypassed_when_asked(self):
        import subprocess
        from aio_system_dashboard.collectors import services
        services.pgrep("^[^ ]*zzcach[e]", max_age_s=5)                              # warm the cache
        p = subprocess.Popen(["bash", "-c", 'exec -a "zzcac""he" sleep 20'])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        time.sleep(0.3)
        self.assertEqual(services.pgrep("^[^ ]*zzcach[e]", max_age_s=0), [p.pid])   # fresh scan sees it


class ParamsCacheTest(unittest.TestCase):
    def test_edited_file_is_read_again_and_unchanged_file_is_not(self):
        from unittest import mock
        from aio_system_dashboard import config as config_mod
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "aio_nav.yaml")
            with open(f, "w") as fh:
                fh.write("aio_nav_node:\n  ros__parameters:\n    ros_domain_id: 10\n")
            self.assertEqual(config_mod.read_aio_nav_params(f)["ros_domain_id"], 10)
            with mock.patch.object(config_mod, "_read_aio_nav_params") as raw:
                self.assertEqual(config_mod.read_aio_nav_params(f)["ros_domain_id"], 10)
                raw.assert_not_called()                                              # served from the cache
            with open(f, "w") as fh:
                fh.write("aio_nav_node:\n  ros__parameters:\n    ros_domain_id: 13\n    use_sim_time: true\n")
            self.assertEqual(config_mod.read_aio_nav_params(f)["ros_domain_id"], 13)  # edit seen
            first = config_mod.read_aio_nav_params(f)
            first["ros_domain_id"] = 99                                              # callers may change it
            self.assertEqual(config_mod.read_aio_nav_params(f)["ros_domain_id"], 13)



class ConcurrentStartTest(unittest.TestCase):
    def test_two_simultaneous_starts_launch_one_process(self):
        import threading as th
        with tempfile.TemporaryDirectory() as root:
            cfgdir = os.path.join(root, "install", "aio_nav_ros", "share", "aio_nav_ros", "config")
            libdir = os.path.join(root, "install", "aio_nav_ros", "lib", "aio_nav_ros")
            os.makedirs(cfgdir)
            os.makedirs(libdir)
            yml = os.path.join(cfgdir, "aio_nav.yaml")
            with open(yml, "w") as f:
                f.write("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n")
            log = os.path.join(root, "launches.txt")
            wrapper = os.path.join(libdir, "aio-nav")
            with open(wrapper, "w") as f:
                f.write(f'#!/bin/bash\necho x >> {log}\nexec -a "zzconcu""rrent" sleep 30\n')
            os.chmod(wrapper, 0o755)
            _isolate_state(self)
            cfg = make(aio_nav_path=yml)
            cfg.data["fake"]["enabled"] = False
            cfg.data["services"]["aio_nav"]["process_pattern"] = "^[^ ]*zzconcurren[t]"
            self.addCleanup(process_control.stop_many, cfg, ["aio_nav"], 2)
            results = []
            ts = [th.Thread(target=lambda: results.append(process_control.start_one(cfg, "aio_nav", stable_s=0.5)))
                  for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(15)
            self.assertTrue(all(r["ok"] for r in results), results)
            with open(log) as f:
                self.assertEqual(len(f.read().split()), 1)       # launched once, not twice


if __name__ == "__main__":
    unittest.main()
