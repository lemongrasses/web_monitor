"""Start-up coordinator (actions/autostart.py)."""

import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.actions import process_control
from aio_system_dashboard.actions import autostart
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


def readiness(store, state="NAV_STARTING", inhibit=(), imu_ok=True, still=True):
    store.set("readiness", {"state": state, "inhibit": list(inhibit),
                            "conditions": [{"name": "imu", "label": "IMU data", "ok": imu_ok}],
                            "motion": {"still": still, "reason": "" if still else "moving: rotation 0.40 rad/s"}})


class AutostartTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: self.state_dir / n),
                  mock.patch.object(autostart, "FALLBACK_FILE", self.state_dir / "shm" / "autostart.json")):
            p.start()
            self.addCleanup(p.stop)
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["allow_control"] = True
        self.data["autostart"].update(enabled=True, prerequisites=["imu"], prerequisite_stable_s=2.0,
                                      retry_backoff_s=[5, 15], max_attempts=3,
                                      require_still=False)      # the still tests switch it on
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

    def test_waits_for_imu_time_without_using_attempts(self):
        c = self.make()
        self.store.set("readiness", {"state": "CHECKING", "inhibit": [], "conditions": [
            {"name": "imu", "label": "IMU data", "ok": False,
             "reason": "IMU time stamps say 1980-01-05 23:59:42 UTC, not a real time; the GNSS receiver "
                       "has no time yet (it needs open sky)"}]})
        for _ in range(30):
            self.tick(c)
        self.assertEqual((self.actions.calls, c.attempts), ([], 0))
        self.assertIn("Waiting for IMU data: IMU time stamps say 1980", c.status()["detail"])
        readiness(self.store)                                         # GNSS time arrived
        for _ in range(3):
            self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])

    def test_never_gives_up_and_the_last_wait_repeats(self):
        self.data["autostart"].update(prerequisites=[], max_attempts=0)
        c = self.make(start_ok=False)
        self.tick(c)
        for wait in (5, 15, 15, 15, 15):                   # backoff [5, 15]: 15 repeats
            self.tick(c, wait - 0.5)
            n = len(self.actions.calls)
            self.tick(c, 1.0)
            self.assertEqual(len(self.actions.calls), n + 1)
        self.assertEqual((c.attempts, c.state), (6, "retry_wait"))
        self.assertIn("next try in 15 s", c.detail)
        self.assertNotIn("gave_up", c.state)

    def test_waits_survive_a_dashboard_restart_in_the_same_power_on(self):
        self.data["autostart"].update(prerequisites=[], max_attempts=0)
        c = self.make(start_ok=False)
        for _ in range(3):
            self.tick(c, 20)                               # three failed attempts
        self.assertEqual(c.attempts, 3)
        c2 = self.make(start_ok=False)                     # dashboard restart
        self.assertEqual(c2.attempts, 3)
        self.tick(c2, 1)
        self.assertEqual(self.actions.calls, [])           # it still waits, no immediate retry
        self.boot = "boot-2"
        self.assertEqual(self.make().attempts, 0)          # a new power-on starts afresh

    def test_retry_with_backoff_then_give_up_when_limited(self):
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
        c2 = self.make()                                   # dashboard restart: still held
        self.tick(c2, 30)
        self.assertEqual(c2.state, "held")
        c2.on_action("start_process", process_control.GROUP)   # operator Start releases it
        self.assertFalse(c2.hold)

    def test_operator_stop_ends_at_the_next_power_on(self):
        self.data["autostart"]["prerequisites"] = []
        c = self.make()
        self.tick(c)
        c.on_action("stop_process", process_control.GROUP)
        self.boot = "boot-2"                               # switched off and on
        c2 = self.make()
        self.assertFalse(c2.hold)
        self.tick(c2)
        self.assertEqual(self.actions.calls, ["start_process"])

    def test_operator_stop_holds_even_when_state_cannot_be_written(self):
        """Found on the device: with state/ read-only the Stop was only in memory, and after a
        dashboard restart automatic start started AIO NAV against the operator's Stop."""
        c = self.make()
        self.tick(c)                                        # started automatically
        blocker = self.state_dir / "autostart.json"
        blocker.mkdir()                                     # cannot be replaced by a file now
        self.actions.run("stop_process", process_control.GROUP)
        self.assertTrue(c.hold)
        self.assertIn("Cannot save the start-up state", self.events.items)
        c2 = self.make()                                    # dashboard restart
        self.assertTrue(c2.hold)
        for _ in range(5):
            self.tick(c2)
        self.assertEqual(self.actions.calls, [])            # still held: nothing started
        blocker.rmdir()                                     # writable again
        self.actions.run("start_process", process_control.GROUP)
        self.assertFalse(c2.hold)
        self.assertFalse(autostart.FALLBACK_FILE.exists())  # the spare copy is gone
        self.assertFalse(self.make().hold)

    def test_after_ready_restarts_once_the_vehicle_is_still(self):
        self.data["autostart"].update(prerequisites=["imu"], require_still=True)
        c = self.make()
        for _ in range(6):
            self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])
        readiness(self.store, "READY")
        self.tick(c)
        self.assertTrue(c.ready_this_boot)
        set_running(self.store, False)                     # AIO NAV dies while driving
        readiness(self.store, "FAULT", inhibit=["NAV_NOT_RUNNING"], still=False)
        for _ in range(30):
            self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])   # not while moving
        self.assertIn("the vehicle to be still (moving: rotation 0.40 rad/s)", c.detail)
        c2 = self.make()                                   # a dashboard restart changes nothing
        readiness(self.store, "FAULT", inhibit=["NAV_NOT_RUNNING"], still=False)
        for _ in range(10):
            self.tick(c2)
        self.assertEqual(self.actions.calls, [])
        readiness(self.store, "FAULT", inhibit=["NAV_NOT_RUNNING"], still=True)   # stopped
        for _ in range(4):
            self.tick(c2)
        self.assertEqual(self.actions.calls, [])           # still for 4 s: not yet 5 s
        self.tick(c2, 2)
        self.tick(c2, 1)
        self.assertEqual(self.actions.calls, ["start_process"])

    def test_first_start_also_waits_for_the_vehicle_to_be_still(self):
        """A watchdog reboot or a power glitch can happen while driving: no start while moving."""
        self.data["autostart"].update(prerequisites=["imu"], require_still=True)
        readiness_moving = lambda: readiness(self.store, "CHECKING", still=False)
        c = self.make()
        readiness_moving()
        for _ in range(20):
            self.tick(c)
        self.assertEqual(self.actions.calls, [])
        self.assertIn("the vehicle to be still", c.detail)
        readiness(self.store, "CHECKING", still=None)         # IMU motion unknown: not still
        for _ in range(10):
            self.tick(c)
        self.assertEqual(self.actions.calls, [])
        readiness(self.store, "CHECKING", still=True)
        for _ in range(7):
            self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])

    def test_no_output_with_missing_inputs_is_left_running(self):
        """Seen on the device: with the sensor drivers stopped AIO NAV kept running and resumed
        by itself when they came back. A restart would not help while the inputs are missing."""
        c = self.make()
        for _ in range(3):
            self.tick(c)
        readiness(self.store, "FAULT", inhibit=["NAV_OUTPUT_STALE"], imu_ok=False)
        for _ in range(60):
            self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])
        self.assertEqual(c.state, "waiting_inputs")

    def test_stopped_output_with_good_inputs_gets_a_grace_period(self):
        self.data["autostart"].update(prerequisites=[], stale_grace_s=10.0)
        c = self.make()
        self.tick(c)
        readiness(self.store, "FAULT", inhibit=["NAV_OUTPUT_STALE"])
        for _ in range(9):
            self.tick(c)
        self.assertEqual((c.state, self.actions.calls), ("recovering", ["start_process"]))
        readiness(self.store, "KEEP_STILL")                # it resumed by itself
        self.tick(c)
        readiness(self.store, "FAULT", inhibit=["NAV_OUTPUT_STALE"])
        for _ in range(9):
            self.tick(c)
        self.assertEqual(self.actions.calls, ["start_process"])   # the grace started again
        self.tick(c, 2)
        self.assertEqual(self.actions.calls, ["start_process", "stop_process"])

    def test_busy_slot_is_not_a_failed_attempt(self):
        """Seen on the device: the DSO watchdog's restart held the process slot; the automatic start
        got 409 and counted it as a failed attempt with a 5 s wait."""
        from aio_system_dashboard.actions.registry import ActionError
        self.data["autostart"]["prerequisites"] = []
        c = self.make()
        real = self.actions.run
        self.actions.run = lambda a, t: (_ for _ in ()).throw(ActionError("another start or stop is still running", 409))
        self.tick(c)
        self.assertEqual((c.attempts, c.state), (0, "retry_wait"))
        self.assertIn("Waiting for another start or stop to finish", c.detail)
        self.actions.run = real
        self.tick(c, 2.5)
        self.assertEqual((self.actions.calls, c.attempts), (["start_process"], 1))

    def test_disabled_without_allow_control(self):
        self.data["nav"]["allow_control"] = False
        c = self.make()
        self.tick(c, 30)
        self.assertEqual((c.state, self.actions.calls, c.status()["enabled"]), ("disabled", [], False))


if __name__ == "__main__":
    unittest.main()
