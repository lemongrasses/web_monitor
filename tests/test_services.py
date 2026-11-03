"""Service states: a unit that systemd keeps restarting is failed (a crash loop), not running."""
import subprocess
import unittest
from unittest import mock

from aio_system_dashboard.collectors import services as sv


class SummarizeTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(sv.summarize({"active": "active", "sub": "running"}, None), "running")
        self.assertEqual(sv.summarize({"active": "activating", "sub": "start"}, None), "running")
        self.assertEqual(sv.summarize({"active": "activating", "sub": "auto-restart"}, None), "failed")
        self.assertEqual(sv.summarize({"active": "active", "sub": "running", "crash_loop": True}, None), "failed")
        self.assertEqual(sv.summarize({"active": "inactive"}, [42]), "running")
        self.assertEqual(sv.summarize({"active": "failed"}, None), "failed")
        self.assertEqual(sv.summarize({"active": "inactive", "load": "not-found"}, None), "not_installed")


class LastMessageTest(unittest.TestCase):
    def test_skips_systemd_lines(self):
        journal = ("Started AIO Aceinna OpenRTK330 ROS 2 driver.\n"
                   "[錯誤] /dev/ttyUSB0 not present; retrying later.\n"
                   "aio-openrtk-ros-driver.service: Main process exited, code=exited, status=1/FAILURE\n"
                   "aio-openrtk-ros-driver.service: Failed with result 'exit-code'.\n"
                   "aio-openrtk-ros-driver.service: Scheduled restart job, restart counter is at 705.\n"
                   "Started AIO Aceinna OpenRTK330 ROS 2 driver.\n")
        with mock.patch.object(sv.subprocess, "run", return_value=mock.Mock(stdout=journal)):
            self.assertEqual(sv.unit_last_message("x.service"), "[錯誤] /dev/ttyUSB0 not present; retrying later.")

    def test_only_systemd_lines_or_nothing(self):
        with mock.patch.object(sv.subprocess, "run", return_value=mock.Mock(stdout="Started x.\n")):
            self.assertEqual(sv.unit_last_message("x.service"), "Started x.")
        with mock.patch.object(sv.subprocess, "run", side_effect=OSError("no journalctl")):
            self.assertEqual(sv.unit_last_message("x.service"), "")
        with mock.patch.object(sv.subprocess, "run", side_effect=subprocess.TimeoutExpired("j", 2)):
            self.assertEqual(sv.unit_last_message("x.service"), "")


class CrashLoopTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        cfg = {"services": {"drivers": {"label": "Sensor drivers", "unit": "drv.service", "optional": True},
                            "cam": {"label": "Camera", "process_pattern": "pylon"}}}
        self.c = sv.ServicesCollector(store=None, cfg=cfg, clock=lambda: self.now)
        self.unit = {"load": "loaded", "active": "active", "sub": "running", "main_pid": 5,
                     "restarts": "700", "since": "", "file_state": "enabled"}
        self.patches = [mock.patch.object(sv, "systemd_unit_state", lambda *a, **k: dict(self.unit)),
                        mock.patch.object(sv, "pgrep", lambda *a, **k: []),
                        mock.patch.object(sv, "unit_last_message", lambda *a, **k: "/dev/ttyUSB0 not present")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_restart_count_rising_is_a_crash_loop(self):
        d = self.c.poll()["drivers"]
        self.assertEqual(d["state"], "running")          # first sample: nothing to compare with
        self.assertFalse(d["unit_state"]["crash_loop"])
        self.now += 2; self.unit["restarts"] = "701"
        d = self.c.poll()["drivers"]
        self.assertEqual(d["state"], "failed")
        self.assertTrue(d["unit_state"]["crash_loop"])
        self.assertEqual(d["unit_state"]["reason"], "/dev/ttyUSB0 not present")
        self.now += 30                                     # still within the window, count unchanged
        self.assertEqual(self.c.poll()["drivers"]["state"], "failed")
        self.now += 40                                     # quiet for over a minute: running again
        d = self.c.poll()["drivers"]
        self.assertEqual(d["state"], "running")
        self.assertFalse(d["unit_state"]["crash_loop"])
        self.assertNotIn("reason", d["unit_state"])

    def test_auto_restart_sample_counts_at_once(self):
        self.unit.update(active="activating", sub="auto-restart", main_pid=0)
        d = self.c.poll()["drivers"]                       # already looping when the dashboard starts
        self.assertEqual(d["state"], "failed")
        self.assertTrue(d["unit_state"]["crash_loop"])
        self.unit.update(active="active", sub="running")   # briefly up again between two crashes
        self.now += 5
        self.assertEqual(self.c.poll()["drivers"]["state"], "failed")

    def test_process_only_service_untouched(self):
        d = self.c.poll()["cam"]
        self.assertEqual(d["state"], "stopped")
        self.assertNotIn("crash_loop", d["unit_state"])

    def test_reason_is_cached(self):
        calls = []
        with mock.patch.object(sv, "unit_last_message", lambda *a, **k: calls.append(1) or "why"):
            self.unit.update(sub="auto-restart", active="activating")
            self.c.poll(); self.now += 2; self.c.poll(); self.now += 2; self.c.poll()
            self.assertEqual(len(calls), 1)
            self.now += 11; self.c.poll()
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
