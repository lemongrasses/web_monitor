import copy
import types
import unittest

from aio_system_dashboard.collectors.dso_watchdog import DsoWatchdog, odometry_finite
from aio_system_dashboard.config import DEFAULTS, Config


class Events:
    def __init__(self):
        self.items = []

    def add(self, level, category, title, detail="", **k):
        self.items.append((level, title))


class Store:
    def __init__(self):
        self.d = {}

    def set(self, k, v):
        self.d[k] = v


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def make(running=True, **over):
    data = copy.deepcopy(DEFAULTS)
    data["nav"]["aio_nav_config"] = ""
    data["dso_watchdog"]["enabled"] = True
    data["dso_watchdog"].update(over)
    cfg = Config(data, None)
    clock, ev, calls = Clock(), Events(), []
    state = {"running": running}

    def restart():
        calls.append(clock.t)
        return {"success": True, "summary": "ok"}
    dog = DsoWatchdog(cfg, Store(), ev, restart, lambda: state["running"], clock)
    # run restarts synchronously so the test is deterministic
    import threading
    dog._thread_target = None
    real = threading.Thread
    threading.Thread = lambda target, **k: types.SimpleNamespace(start=target)
    return dog, clock, ev, calls, state, lambda: setattr(threading, "Thread", real)


def odom(**vals):
    def vec(**kw):
        return types.SimpleNamespace(x=kw.get("x", 0.0), y=kw.get("y", 0.0), z=kw.get("z", 0.0),
                                     w=kw.get("w", 1.0))
    m = types.SimpleNamespace(
        pose=types.SimpleNamespace(pose=types.SimpleNamespace(position=vec(), orientation=vec())),
        twist=types.SimpleNamespace(twist=types.SimpleNamespace(linear=vec(), angular=vec())))
    if "px" in vals:
        m.pose.pose.position.x = vals["px"]
    if "wz" in vals:
        m.twist.twist.angular.z = vals["wz"]
    return m


class OdometryTest(unittest.TestCase):
    def test_finite_and_nan(self):
        nan = float("nan")
        self.assertTrue(odometry_finite(odom()))
        self.assertFalse(odometry_finite(odom(px=nan)))
        self.assertFalse(odometry_finite(odom(wz=float("inf"))))


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.restore = None

    def tearDown(self):
        if self.restore:
            self.restore()

    def build(self, **kw):
        dog, clock, ev, calls, state, self.restore = make(**kw)
        return dog, clock, ev, calls, state

    def test_healthy_odometry_never_restarts(self):
        dog, clock, ev, calls, _ = self.build()
        for _ in range(20):
            dog.on_message(True)
            clock.t += 1
            dog.tick()
        self.assertEqual((dog.state, calls), ("ok", []))

    def test_nan_restarts_dso_only_after_enough_bad_messages(self):
        dog, clock, ev, calls, _ = self.build()
        dog.on_message(True); dog.tick()
        dog.on_message(False); dog.on_message(False); dog.tick()
        self.assertEqual(calls, [])             # 2 < bad_messages (3)
        dog.on_message(False); dog.tick()
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("restarting DSO" in t for _, t in ev.items))

    def test_isolated_nan_between_good_messages_does_not_restart(self):
        dog, clock, ev, calls, _ = self.build()
        for _ in range(5):
            dog.on_message(False); dog.on_message(True); dog.tick()
        self.assertEqual(calls, [])

    def test_settle_and_cooldown(self):
        dog, clock, ev, calls, _ = self.build(settle_s=15, cooldown_s=30)
        for _ in range(3):
            dog.on_message(False)
        dog.tick()
        self.assertEqual(len(calls), 1)
        for _ in range(3):
            dog.on_message(False)               # still NaN right after the restart
        clock.t += 5; dog.tick()
        self.assertEqual((dog.state, len(calls)), ("settling", 1))
        clock.t += 20                           # settled, cooldown (30 s) not over yet
        for _ in range(3):
            dog.on_message(False)
        dog.tick()
        self.assertEqual((dog.state, len(calls)), ("bad", 1))
        clock.t += 15
        dog.tick()
        self.assertEqual(len(calls), 2)

    def test_gives_up_after_max_restarts_and_recovers(self):
        dog, clock, ev, calls, _ = self.build(settle_s=0, cooldown_s=0, max_restarts=2, window_s=600)
        for _ in range(4):
            for _ in range(3):
                dog.on_message(False)
            clock.t += 1
            dog.tick()
        self.assertEqual((dog.state, len(calls)), ("gave_up", 2))
        self.assertEqual(sum(1 for _, t in ev.items if "gave up" in t), 1)   # logged once
        dog.on_message(True); clock.t += 1; dog.tick()
        self.assertEqual(dog.state, "ok")

    def test_does_nothing_while_dso_is_stopped(self):
        dog, clock, ev, calls, state = self.build()
        state["running"] = False
        for _ in range(5):
            dog.on_message(False)
        dog.tick()
        self.assertEqual((dog.state, calls), ("idle", []))

    def test_disabled(self):
        dog, clock, ev, calls, _ = self.build(enabled=False)
        for _ in range(5):
            dog.on_message(False)
        dog.tick()
        self.assertEqual((dog.state, calls), ("disabled", []))


if __name__ == "__main__":
    unittest.main()
