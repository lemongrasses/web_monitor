import copy
import threading
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


class Rig:
    """A watchdog with fake DSO control; actions run synchronously and take no time."""

    def __init__(self, test, running=True, wanted=True, **over):
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["dso_watchdog"]["enabled"] = True
        data["dso_watchdog"].update(over)
        self.clock, self.events = Clock(), Events()
        self.restarts, self.starts = [], []
        self.running, self.wanted = running, wanted

        def restart():
            self.restarts.append(self.clock.t)
            return {"success": True, "summary": "ok"}

        def start():
            self.starts.append(self.clock.t)
            self.running = self.starts_ok
            return {"success": self.starts_ok, "summary": ""}
        self.starts_ok = False
        self.mem = None                            # DSO's RAM + swap in MB (None: not measured)
        self.dog = DsoWatchdog(Config(data, None), Store(), self.events, restart, start,
                               lambda: self.running, lambda: self.wanted,
                               lambda: setattr(self, "wanted", True), self.clock,
                               memory_mb=lambda: self.mem, total_memory_mb=16000.0)
        real = threading.Thread
        threading.Thread = lambda target, args=(), **k: types.SimpleNamespace(start=lambda: target(*args))
        test.addCleanup(setattr, threading, "Thread", real)

    def nan(self, n=1):
        for _ in range(n):
            self.dog.on_message(False)

    def tick(self, dt=0.0):
        self.clock.t += dt
        self.dog.tick()
        return self.dog.state


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
        self.assertTrue(odometry_finite(odom()))
        self.assertFalse(odometry_finite(odom(px=float("nan"))))
        self.assertFalse(odometry_finite(odom(wz=float("inf"))))


class NanTest(unittest.TestCase):
    def test_healthy_odometry_never_restarts(self):
        r = Rig(self)
        for _ in range(20):
            r.dog.on_message(True)
            r.tick(1)
        self.assertEqual((r.dog.state, r.restarts, r.starts), ("ok", [], []))

    def test_one_nan_restarts_dso_at_once(self):
        r = Rig(self)
        r.dog.on_message(True); r.tick()
        r.nan(); r.tick()
        self.assertEqual(len(r.restarts), 1)
        self.assertEqual(r.starts, [])                         # AIO NAV is never involved
        self.assertTrue(any("restarting DSO" in t for _, t in r.events.items))

    def test_a_stricter_threshold_can_be_configured(self):
        r = Rig(self, bad_messages=3)
        r.nan(2); r.tick()
        self.assertEqual(r.restarts, [])
        r.nan(); r.tick()
        self.assertEqual(len(r.restarts), 1)

    def test_settles_after_a_restart_then_restarts_again_if_still_nan(self):
        r = Rig(self, settle_s=3, cooldown_s=2)
        r.nan(); r.tick()
        r.nan()
        self.assertEqual(r.tick(1), "settling")                # old messages are ignored
        self.assertEqual(len(r.restarts), 1)
        r.clock.t += 5
        r.nan(); r.tick()
        self.assertEqual(len(r.restarts), 2)

    def test_repeated_nan_slows_down_to_the_retry_interval(self):
        r = Rig(self, settle_s=0, cooldown_s=2, fast_restarts=3, window_s=60, retry_s=30)
        for _ in range(3):
            r.nan(); r.tick(3)
        self.assertEqual(len(r.restarts), 3)
        r.nan()
        self.assertEqual(r.tick(10), "bad")                    # 3 restarts in the window: wait 30 s
        self.assertEqual(len(r.restarts), 3)
        r.tick(25)
        self.assertEqual(len(r.restarts), 4)


class DownTest(unittest.TestCase):
    def test_crashed_dso_is_started_every_30_s(self):
        r = Rig(self, running=False, wanted=True, retry_s=30, settle_s=0)
        self.assertEqual(r.tick(), "retrying")                 # first sighting: wait, do not hammer
        r.tick(29)
        self.assertEqual(r.starts, [])
        r.tick(2)                                              # 31 s after it went down
        self.assertEqual(len(r.starts), 1)
        self.assertEqual(r.tick(10), "retrying")               # 10 s after the failed try
        r.tick(15)                                             # 25 s after it
        self.assertEqual(len(r.starts), 1)
        r.tick(10)                                             # 35 s after it
        self.assertEqual(len(r.starts), 2)

    def test_stops_trying_once_dso_stays_up(self):
        r = Rig(self, running=False, wanted=True, retry_s=30, settle_s=0)
        r.starts_ok = True
        r.tick(); r.tick(31)
        self.assertEqual(len(r.starts), 1)
        self.assertTrue(r.running)
        r.dog.on_message(True)
        self.assertEqual(r.tick(1), "ok")
        r.tick(100)
        self.assertEqual(len(r.starts), 1)

    def test_a_dso_stopped_on_purpose_stays_stopped(self):
        r = Rig(self, running=False, wanted=False)
        for _ in range(5):
            self.assertEqual(r.tick(60), "idle")
        self.assertEqual((r.starts, r.restarts), ([], []))

    def test_a_running_dso_counts_as_wanted(self):
        r = Rig(self, running=True, wanted=False)
        r.tick()
        self.assertTrue(r.wanted)

    def test_outage_is_logged_once(self):
        r = Rig(self, running=False, wanted=True, retry_s=30, settle_s=0)
        r.tick()
        for _ in range(3):
            r.tick(31)
        self.assertEqual(len(r.starts), 3)
        self.assertEqual(sum(1 for _, t in r.events.items if "not running" in t), 1)

    def test_disabled(self):
        r = Rig(self, running=False, wanted=True, enabled=False)
        r.tick(100)
        self.assertEqual((r.dog.state, r.starts, r.restarts), ("disabled", [], []))


class MemoryGuardTest(unittest.TestCase):
    """DSO growing without bound (it kept every camera frame) is restarted before the machine hangs."""

    def test_restart_above_the_limit_only(self):
        r = Rig(self)
        self.assertEqual(r.dog.memory_limit_mb, 3200.0)            # auto: 20% of RAM
        r.mem = 3000
        r.dog.tick()
        self.assertEqual(r.restarts, [])
        r.mem = 3300
        r.dog.tick()
        self.assertEqual(len(r.restarts), 1)
        self.assertIn(("fault", "DSO used too much memory: restarting DSO"), r.events.items)
        self.assertEqual(r.dog.status()["memory_restarts"], 1)
        self.assertEqual(r.dog.status()["memory_mb"], 3300)

    def test_memory_wins_over_settling(self):
        r = Rig(self)
        r.dog._settle_until = r.clock.t + 100                     # just started: odometry ignored ...
        r.mem = 9000
        r.dog.tick()
        self.assertEqual(len(r.restarts), 1)                       # ... but memory is not

    def test_configurable_and_off(self):
        r = Rig(self, max_memory_mb=1000)
        self.assertEqual(r.dog.memory_limit_mb, 1000.0)
        r = Rig(self, max_memory_mb=0)
        r.mem = 99999
        r.dog.tick()
        self.assertEqual(r.restarts, [])

    def test_not_running_is_not_measured(self):
        r = Rig(self, running=False, wanted=False)
        r.mem = 99999
        r.dog.tick()
        self.assertEqual(r.restarts, [])



class MemoryMeasureTest(unittest.TestCase):
    def test_counts_a_real_process(self):
        import subprocess, sys, time
        from aio_system_dashboard.actions import process_control
        code = "x = bytearray(150 * 1024 * 1024); import time; time.sleep(30)  # aio-memtest"
        proc = subprocess.Popen([sys.executable, "-c", code])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["services"] = {"memtest": {"process_pattern": "aio-memtest"}}
        cfg = Config(data, None)
        mb = None
        for _ in range(30):
            mb = process_control.memory_mb(cfg, "memtest")
            if mb and mb > 150:
                break
            time.sleep(0.1)
        self.assertGreater(mb, 150)
        self.assertLess(mb, 400)
        self.assertGreater(process_control.total_memory_mb(), 1000)

if __name__ == "__main__":
    unittest.main()
