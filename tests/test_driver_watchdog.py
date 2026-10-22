"""Sensor driver watchdog (actions/driver_watchdog.py) and the systemd alive signal (sdnotify)."""

import copy
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard import sdnotify
from aio_system_dashboard.actions.driver_watchdog import DriverWatchdog
from aio_system_dashboard.config import DEFAULTS, Config, drivers_service
from aio_system_dashboard.state.store import StateStore


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Events:
    def __init__(self):
        self.items = []

    def add(self, *a, **k):
        self.items.append(a[2] if len(a) > 2 else "")


class FakeActions:
    def __init__(self):
        self.calls = []

    def run(self, action_id, target):
        self.calls.append((action_id, target))
        return {"success": True, "summary": "restarted"}


class DriverWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.mode = "live"
        p = mock.patch.object(Config, "ros_mode", lambda _s: self.mode)
        p.start()
        self.addCleanup(p.stop)
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["services"] = {"drivers": {"label": "Sensor drivers", "user_unit": "drivers.service"}}
        data["ros"]["topics"] = [{"name": "/imu", "group": "nav"}]
        data["driver_watchdog"].update(stale_s=45.0, retry_backoff_s=[60, 120])
        self.store, self.clock, self.events, self.actions = StateStore(), Clock(), Events(), FakeActions()
        self.dog = DriverWatchdog(Config(data, None), self.store, self.actions, self.events, clock=self.clock)
        self.unit("active")
        self.imu("healthy")
        real = threading.Thread                       # restarts run at once in the tests
        threading.Thread = lambda target, **k: type("T", (), {"start": lambda self: target()})()
        self.addCleanup(setattr, threading, "Thread", real)

    def unit(self, active):
        self.store.set("services", {"drivers": {"label": "Sensor drivers", "unit_state": {"active": active}}})

    def imu(self, state):
        self.store.set("ros", {"available": True, "watched": [{"name": "/imu", "group": "nav", "state": state}]})

    def run_for(self, seconds, step=2.0):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            self.dog.tick()

    def test_stuck_driver_is_restarted_with_growing_waits(self):
        self.imu("stale")
        self.run_for(40)
        self.assertEqual(self.actions.calls, [])                 # not 45 s yet
        self.run_for(8)
        self.assertEqual(self.actions.calls, [("restart_driver", "drivers")])
        self.assertIn("Sensor drivers restarted", self.events.items)
        self.run_for(58)                                          # 45 s again, but the wait is 60 s
        self.assertEqual(len(self.actions.calls), 1)
        self.run_for(4)
        self.assertEqual(len(self.actions.calls), 2)
        self.run_for(110)
        self.assertEqual(len(self.actions.calls), 2)              # now 120 s
        self.run_for(12)
        self.assertEqual(len(self.actions.calls), 3)

    def test_a_healthy_imu_starts_the_waits_afresh(self):
        self.imu("missing")
        self.run_for(50)
        self.assertEqual(self.dog.restarts, 1)
        self.imu("healthy")
        self.run_for(4)
        self.assertEqual(self.dog.restarts, 0)
        self.imu("missing")
        self.run_for(50)
        self.assertEqual(len(self.actions.calls), 2)              # no 60 s wait carried over

    def test_bag_mode_operator_stop_and_inactive_unit_are_left_alone(self):
        self.imu("missing")
        self.mode = "bag"
        self.run_for(200)
        self.mode = "live"
        self.dog.on_action("stop_driver", "drivers")
        self.run_for(200)
        self.dog.on_action("start_driver", "drivers")
        self.unit("activating")                                   # systemd's own restart
        self.run_for(200)
        self.assertEqual(self.actions.calls, [])

    def test_a_failed_unit_is_started_again(self):
        self.unit("failed")
        self.imu("missing")
        self.run_for(40)
        self.assertEqual(self.actions.calls, [])
        self.run_for(8)
        self.assertEqual(self.actions.calls, [("restart_driver", "drivers")])
        self.assertEqual((self.dog.state, self.dog.restarts_total), ("restarted", 1))

    def test_imu_without_valid_time_is_not_a_driver_fault(self):
        self.imu("healthy")                                       # publishes, time_ok False
        self.store.set("ros", {"available": True, "watched": [
            {"name": "/imu", "group": "nav", "state": "healthy", "time_ok": False}]})
        self.run_for(200)
        self.assertEqual(self.actions.calls, [])

    def test_the_camera_unit_is_never_taken_for_the_driver_stack(self):
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["services"] = {"camera": {"label": "Camera driver", "user_unit": "cam.service", "restartable": True},
                            "drivers": {"label": "Sensor drivers", "user_unit": "drivers.service"}}
        cfg = Config(data, None)
        self.assertEqual(drivers_service(cfg), "drivers")
        self.assertEqual(DriverWatchdog(cfg, self.store, self.actions, self.events).service, "drivers")
        del data["services"]["drivers"]
        self.assertEqual(drivers_service(Config(data, None)), "camera")    # only one: still found


class SdNotifyTest(unittest.TestCase):
    def test_sends_to_the_notify_socket(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "notify")
            rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            rx.bind(path)
            self.addCleanup(rx.close)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "60000000"}):
                self.assertTrue(sdnotify.notify("WATCHDOG=1"))
                self.assertEqual(sdnotify.watchdog_interval_s(), 10.0)
            self.assertEqual(rx.recv(64), b"WATCHDOG=1")

    def test_without_systemd_nothing_is_sent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sdnotify.notify("WATCHDOG=1"))
            self.assertEqual(sdnotify.watchdog_interval_s(), 0.0)


class AliveTest(unittest.TestCase):
    def test_alive_only_while_the_loops_turn(self):
        now = 1000.0
        self.assertTrue(sdnotify.loops_alive([999.0, 995.0], now))
        self.assertFalse(sdnotify.loops_alive([999.0, now - 60], now))    # one loop hangs
        self.assertFalse(sdnotify.loops_alive([None], now))               # never ran
        self.assertTrue(sdnotify.loops_alive([], now))


if __name__ == "__main__":
    unittest.main()
