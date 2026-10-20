"""Start-up coordinator (actions/autostart.py)."""

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.actions import process_control
from aio_system_dashboard.actions.autostart import StartupCoordinator
from aio_system_dashboard.config import DEFAULTS, Config
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
    """Records starts/stops and makes the fake process appear or disappear."""

    def __init__(self, store, start_ok=True):
        self.store, self.start_ok, self.calls = store, start_ok, []
        self.coordinator = None

    def run(self, action_id, target):
        self.calls.append(action_id)
        running = action_id == "start_process" and self.start_ok
        set_running(self.store, running)
        if self.coordinator:                               # like ActionRegistry.after_run
            self.coordinator.on_action(action_id, target)
        return {"success": running or action_id == "stop_process", "summary": "fake"}


def set_running(store, running):
    store.set("services", {"aio_nav": {"pids": [4242] if running else []}})


def readiness(store, state="NAV_STARTING", inhibit=(), imu_ok=True):
    store.set("readiness", {"state": state, "inhibit": list(inhibit),
                            "conditions": [{"name": "imu", "label": "IMU data", "ok": imu_ok}]})


class AutostartTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        p = mock.patch.object(Config, "state_file", lambda _s, n: self.state_dir / n)
        p.start()
        self.addCleanup(p.stop)
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["allow_control"] = True
        self.data["autostart"].update(enabled=True, prerequisites=["imu"], prerequisite_stable_s=2.0,
                                      retry_backoff_s=[5, 15], max_attempts=3)
        self.data["services"] = {"aio_nav": {"label": "AIO NAV", "launch": "aio-nav"}}
        self.boot = "boot-1"

    def make(self, start_ok=True):
        self.store, self.clock, self.events = StateStore(), Clock(), Events()
        set_running(self.store, False)
        readiness(self.store)
        self.actions = FakeActions(self.store, start_ok)
        c = StartupCoordinator(Config(self.data, None), self.store, self.actions, self.events,
                               clock=self.clock, boot_id=lambda: self.boot)
        self.actions.coordinator = c
        return c

    def tick(self, c, seconds=1.0):
        self.clock.t += seconds
        c.tick()
        for _ in range(200):                               # let a launch thread finish
            if not c._launching:
                break
            time.sleep(0.005)

    def test_waits_for_stable_prerequisites_then_starts_once(self):
        c = self.make()
        readiness(self.store, "CHECKING", imu_ok=False)
        self.tick(c)
        self.assertEqual((c.state, self.actions.calls), ("waiting", []))
        self.assertIn("IMU data", c.detail)
        readiness(self.store, "CHECKING", imu_ok=True)
        self.tick(c)
        self.tick(c, 1.0)
        self.assertEqual(self.actions.calls, [])          # 1 s of 2 s stable
        self.tick(c, 1.5)
        self.assertEqual(self.actions.calls, ["start_process"])
        self.tick(c)
        self.tick(c)
        self.assertEqual((c.state, self.actions.calls), ("running", ["start_process"]))   # adopted, not doubled

    def test_retry_with_backoff_then_give_up(self):
        self.data["autostart"]["prerequisites"] = []
        c = self.make(start_ok=False)
        self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])
        self.assertEqual(c.state, "retry_wait")
        self.tick(c, 4.0)
        self.assertEqual(len(self.actions.calls), 1)      # backoff 5 s not over
        self.tick(c, 1.5)
        self.assertEqual(len(self.actions.calls), 2)
        self.tick(c, 16.0)
        self.assertEqual(len(self.actions.calls), 3)
        self.tick(c, 30.0)
        self.assertEqual((c.state, len(self.actions.calls)), ("gave_up", 3))

    def test_no_output_is_stopped_and_retried(self):
        self.data["autostart"]["prerequisites"] = []
        c = self.make()
        self.tick(c)
        readiness(self.store, "FAULT", inhibit=["STARTUP_TIMEOUT"])
        self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process", "stop_process"])
        self.assertFalse(c.hold)                           # its own stop is not an operator hold
        self.tick(c, 6.0)
        self.assertEqual(self.actions.calls[-1], "start_process")

    def test_own_stop_is_not_judged_again_while_the_snapshots_lag(self):
        # On the device the services snapshot showed the stopped run for a few seconds more: the
        # retry was scheduled and AIO NAV stopped three times. Here the store keeps the old run.
        self.data["autostart"]["prerequisites"] = []
        c = self.make()
        self.tick(c)
        readiness(self.store, "FAULT", inhibit=["STARTUP_TIMEOUT"])
        self.actions.run = lambda action_id, target: (self.actions.calls.append(action_id),
                                                      {"success": True, "summary": "fake"})[1]
        self.tick(c)
        for _ in range(3):
            self.tick(c, 1.0)
        self.assertEqual(self.actions.calls, ["start_process", "stop_process"])
        self.assertEqual(self.events.items.count("Automatic start: retry scheduled"), 1)
        set_running(self.store, False)                    # the snapshots catch up
        readiness(self.store, "CHECKING")
        self.tick(c, 3.0)
        self.assertEqual(self.actions.calls[-1], "start_process")
        self.assertEqual(c.attempts, 2)

    def test_operator_stop_holds_across_a_restart(self):
        self.data["autostart"]["prerequisites"] = []
        c = self.make()
        self.tick(c)
        c.on_action("stop_process", process_control.GROUP)
        set_running(self.store, False)
        self.tick(c, 30)
        self.assertEqual((c.state, self.actions.calls), ("held", ["start_process"]))
        c2 = self.make()                                   # power cycle: still held
        self.tick(c2, 30)
        self.assertEqual(c2.state, "held")
        c2.on_action("start_process", process_control.GROUP)   # operator Start releases it
        self.assertFalse(c2.hold)

    def test_never_restarts_after_ready_in_this_boot(self):
        self.data["autostart"]["prerequisites"] = []
        c = self.make()
        self.tick(c)
        readiness(self.store, "READY")
        self.tick(c)
        set_running(self.store, False)                     # AIO NAV dies while driving
        readiness(self.store, "FAULT", inhibit=["NAV_NOT_RUNNING"])
        self.tick(c, 60)
        self.assertEqual((c.state, self.actions.calls), ("ready_this_boot", ["start_process"]))
        c2 = self.make()                                   # dashboard restart, same boot
        self.tick(c2, 60)
        self.assertEqual(self.actions.calls, [])
        self.boot = "boot-2"                               # next power-on starts again
        c3 = self.make()
        self.tick(c3)
        self.assertEqual(self.actions.calls, ["start_process"])

    def test_disabled_without_allow_control(self):
        self.data["nav"]["allow_control"] = False
        c = self.make()
        self.tick(c, 30)
        self.assertEqual((c.state, self.actions.calls, c.status()["enabled"]), ("disabled", [], False))


if __name__ == "__main__":
    unittest.main()
