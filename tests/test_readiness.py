"""Readiness: the pure evaluator, condition hysteresis, and the health engine that feeds it."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.state import readiness as rd
from aio_system_dashboard.state import still
from aio_system_dashboard.state.health import HealthEngine
from aio_system_dashboard.state.store import StateStore

ALIGNED = {"alignment": True, "heading_valid": True, "fine_alignment": True, "flags_c": 0xE8}


def ev(**kw):
    base = {"product_state": "READY", "nav_running": True, "nav_fresh": True, "nav_flags": ALIGNED,
            "ever_output": True, "config_ok": True}
    base.update(kw)
    return rd.evaluate(base)


class EvaluateTest(unittest.TestCase):
    def test_default_ready_is_the_product_ready(self):
        r = ev()
        self.assertEqual((r["state"], r["requested_action"], r["inhibit_flags"]), ("READY", "NONE", 0))
        for bit in ("NAV_PROCESS_OK", "NAV_OUTPUT_OK", "ALIGNMENT_OK", "HEADING_OK", "FINE_ALIGNMENT_OK"):
            self.assertTrue(r["health_flags"] >> rd.HEALTH_BITS[bit] & 1, bit)

    def test_alignment_phases_and_actions(self):
        for key, state, action, bit in (("alignment", "KEEP_STILL", "HOLD_STILL", "KEEP_STILL_REQUIRED"),
                                        ("heading_valid", "MOVE_STRAIGHT", "DRIVE_STRAIGHT", "MOVE_STRAIGHT_REQUIRED"),
                                        ("fine_alignment", "REFINING", "KEEP_DRIVING", "FINE_ALIGNMENT_REQUIRED")):
            r = ev(product_state="INITIALIZING", step={"key": key})
            self.assertEqual((r["state"], r["requested_action"]), (state, action))
            self.assertEqual(r["inhibit"], [bit])
            self.assertEqual(r["state_code"], rd.STATES.index(state))

    def test_output_before_alignment_never_authorises(self):
        """0x04 flows long before alignment; only READY clears every inhibit bit."""
        for st, step in (("STARTING", None), ("INITIALIZING", {"key": "alignment"}), ("FAULT", None),
                         ("STOPPED", None), ("UNKNOWN", None)):
            r = ev(product_state=st, step=step, nav_running=st != "STOPPED")
            self.assertNotEqual(r["state"], "READY")
            self.assertNotEqual(r["inhibit_flags"], 0, st)

    def test_required_condition_holds_ready_back(self):
        c = {"gnss_quality": {"required": True, "ok": False, "ready": False, "reason": "GNSS SPP"}}
        r = ev(conditions=c)
        self.assertEqual((r["state"], r["requested_action"]), ("REFINING", "KEEP_DRIVING"))
        self.assertEqual(r["inhibit"], ["GNSS_QUALITY_LOW"])
        self.assertIn("GNSS SPP", r["reasons"])
        c["gnss_quality"]["required"] = False                      # advisory: READY, health bit off
        r = ev(conditions=c)
        self.assertEqual(r["state"], "READY")
        self.assertFalse(r["health_flags"] >> rd.HEALTH_BITS["GNSS_QUALITY_OK"] & 1)

    def test_degraded_after_ready_asks_for_a_safe_stop(self):
        lost = {"dso": {"required": True, "ok": False, "ready": False, "reason": "DSO odometry bad"}}
        r = ev(conditions=lost, ever_ready=True)
        self.assertEqual((r["state"], r["requested_action"]), ("DEGRADED", "SAFE_STOP"))
        r = ev(product_state="INITIALIZING", step={"key": "alignment"}, ever_ready=True)
        self.assertEqual(r["state"], "DEGRADED")                   # alignment lost: never back to KEEP_STILL
        r = ev(product_state="FAULT", ever_ready=True, nav_fresh=False)
        self.assertEqual((r["state"], r["requested_action"]), ("FAULT", "SAFE_STOP"))

    def test_hold_autostart_ownership(self):
        r = ev(product_state="STOPPED", nav_running=False, hold=True)
        self.assertEqual((r["state"], r["requested_action"]), ("MAINTENANCE", "MAINTENANCE_HOLD"))
        self.assertIn("MAINTENANCE_HOLD", r["inhibit"])
        r = ev(product_state="STOPPED", nav_running=False, autostart={"enabled": True, "state": "waiting",
                                                                      "detail": "Waiting for IMU data"})
        self.assertEqual((r["state"], r["requested_action"]), ("CHECKING", "HOLD_STILL"))
        r = ev(ownership="AIO NAV is running twice")
        self.assertEqual((r["state"], r["requested_action"]), ("FAULT", "SERVICE_REQUIRED"))
        self.assertIn("OWNERSHIP_CONFLICT", r["inhibit"])
        r = ev(product_state="STOPPED", nav_running=False, autostart={"enabled": True, "state": "gave_up"})
        self.assertIn("STARTUP_TIMEOUT", r["inhibit"])

    def test_automatic_failures_say_what_happens_instead_of_asking_for_service(self):
        auto = {"enabled": True, "state": "recovering",
                "detail": "AIO NAV navigation output stopped; restarting it in 6 s if it does not resume"}
        r = ev(product_state="FAULT", nav_fresh=False, product_reasons=["Navigation output stopped", "x"],
               autostart=auto)
        self.assertEqual((r["state"], r["requested_action"]), ("FAULT", "HOLD_STILL"))
        self.assertEqual(r["reasons"], ["Navigation output stopped", auto["detail"]])
        r = ev(product_state="FAULT", nav_fresh=False, product_reasons=["Navigation output stopped"],
               autostart=auto, ever_ready=True)
        self.assertEqual(r["requested_action"], "SAFE_STOP")          # after READY: stop first
        r = ev(product_state="FAULT", nav_running=False, nav_fresh=False, needs_service=True,
               product_reasons=["AIO NAV is not installed on this device"], autostart=auto)
        self.assertEqual(r["requested_action"], "SERVICE_REQUIRED")   # a person is needed
        r = ev(product_state="FAULT", nav_fresh=False, product_reasons=["Navigation output stopped"],
               autostart={"enabled": False})
        self.assertEqual(r["requested_action"], "SERVICE_REQUIRED")   # manual mode as before

    def test_crash_after_ready_in_automatic_mode_says_how_it_recovers(self):
        auto = {"enabled": True, "state": "waiting",
                "detail": "Waiting for the vehicle to be still (moving: rotation 0.40 rad/s)"}
        r = ev(product_state="FAULT", nav_running=False, nav_fresh=False, ever_ready=True, wanted=False,
               autostart=auto)
        self.assertEqual((r["state"], r["requested_action"]), ("FAULT", "SAFE_STOP"))
        self.assertEqual(r["reasons"], ["AIO NAV stopped after it was ready", auto["detail"]])

    def test_coordinator_starting_is_nav_starting(self):
        auto = {"enabled": True, "state": "starting", "detail": "Starting AIO NAV (attempt 1)"}
        r = ev(product_state="FAULT", nav_running=False, nav_fresh=False,
               product_reasons=["AIO NAV stopped unexpectedly"], autostart=auto)
        self.assertEqual((r["state"], r["requested_action"], r["reasons"]),
                         ("CHECKING", "HOLD_STILL", ["Starting AIO NAV (attempt 1)"]))
        # the process is there, the (debounced) product state still says it stopped
        r = ev(product_state="FAULT", nav_running=True, nav_fresh=False, ever_output=False,
               product_reasons=["AIO NAV stopped unexpectedly"], autostart=auto)
        self.assertEqual((r["state"], r["requested_action"], r["reasons"]),
                         ("NAV_STARTING", "HOLD_STILL", ["Starting AIO NAV (attempt 1)"]))

    def test_not_ready_always_has_an_inhibit_bit(self):
        """The vehicle may check only inhibit_flags == 0, so every other state must set a bit,
        whatever combination of inputs (the product state is debounced and can lag)."""
        import itertools
        for st, run, fresh, flags, ever in itertools.product(
                ("UNKNOWN", "STOPPED", "STARTING", "INITIALIZING", "READY", "FAULT"),
                (True, False), (True, False), ({}, ALIGNED, {"alignment": True}), (True, False)):
            r = ev(product_state=st, nav_running=run, nav_fresh=fresh, nav_flags=flags, ever_ready=ever,
                   step={"key": "alignment"} if st == "INITIALIZING" else None)
            if r["state"] == "READY":
                self.assertEqual(r["inhibit_flags"], 0)
                self.assertTrue(fresh and run and flags == ALIGNED, (st, run, fresh, flags))
            else:
                self.assertNotEqual(r["inhibit_flags"], 0, (st, run, fresh, flags, ever))

    def test_boot_comes_first_even_while_autostart_waits(self):
        r = ev(product_state="UNKNOWN", nav_running=False, booting=True,
               autostart={"enabled": True, "state": "waiting"})
        self.assertEqual(r["state"], "BOOTING")

    def test_degraded_text_does_not_repeat_the_alignment_prompt(self):
        r = ev(product_state="INITIALIZING", step={"key": "heading_valid"}, ever_ready=True,
               product_reasons=["Step 2 of 3: Initial heading", "Drive straight at a steady speed."])
        self.assertEqual(r["reasons"], ["Alignment lost after it was ready"])

    def test_unknown_required_condition_fails_closed(self):
        r = ev(unknown_required=["gnss-quality"])
        self.assertNotEqual(r["state"], "READY")
        self.assertIn("CONFIG_INVALID", r["inhibit"])
        self.assertIn("unknown condition 'gnss-quality'", r["reasons"][0])

    def test_masks_round_trip(self):
        r = ev(product_state="INITIALIZING", step={"key": "heading_valid"}, nav_fresh=True)
        self.assertEqual(rd.names_of(r["inhibit_flags"], rd.INHIBIT_BITS), r["inhibit"])

    def test_gnss_rank_counts_unrated_as_float(self):
        need = rd.MIN_QUALITY["float"]
        self.assertGreaterEqual(rd.gnss_rank("unrated"), need)     # status -1 inside float periods
        self.assertLess(rd.gnss_rank("unrated_poor"), need)
        self.assertLess(rd.gnss_rank("spp"), need)
        self.assertGreater(rd.gnss_rank("fixed"), need)


class ConditionTest(unittest.TestCase):
    def test_stable_before_ready_and_short_dropouts_ignored(self):
        c = rd.Condition(stable_s=2.0, loss_s=1.0)
        self.assertFalse(c.update(True, 0.0))
        self.assertFalse(c.update(True, 1.9))
        self.assertTrue(c.update(True, 2.0))
        self.assertTrue(c.update(False, 2.5))                       # dropout shorter than loss_s
        self.assertTrue(c.update(True, 3.0))
        self.assertTrue(c.update(False, 4.0))
        self.assertFalse(c.update(False, 5.0))                      # lost after loss_s
        self.assertFalse(c.update(True, 5.5))                       # must be stable again
        self.assertTrue(c.update(True, 7.5))

    def test_never_ready_condition_drops_at_once(self):
        c = rd.Condition(stable_s=2.0, loss_s=1.0)
        c.update(True, 0.0)
        c.update(False, 1.0)
        self.assertFalse(c.update(True, 2.5))                       # the stable time restarted at 1.0


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeNav:
    """Stands in for NavUdpCollector: what the engine reads of it."""

    def __init__(self):
        self.session = 1
        self.latest = None
        self.age = None

    def status(self):
        return {"listening": True, "bind_error": None, "age_s": self.age, "rate_hz": 100.0 if self.age is not None else 0,
                "latest": dict(self.latest) if self.latest else None, "flag_age_s": {}, "session": self.session,
                "session_started": 1.0 if self.latest else None, "packets": 0, "bad_packets": 0}

    def new_session(self, reason):
        self.session += 1

    def clear(self, reason):
        self.session += 1
        self.latest = None
        self.age = None

    def is_clear(self):
        return self.latest is None


class Events:
    def __init__(self):
        self.items = []

    def add(self, level, category, title, detail="", **k):
        self.items.append((category, title))


class EngineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        cfg_yaml = self.tmp / "aio_nav.yaml"
        cfg_yaml.write_text("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n"
                            "    output_udp: '192.0.2.10:9000'\n    Odometry:\n      vupt: true\n")
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: self.tmp / "state" / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: self.tmp / "state" / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["aio_nav_config"] = str(cfg_yaml)
        self.data["services"] = {"aio_nav": {"label": "AIO NAV", "unit": "aio-nav.service",
                                             "process_pattern": "x/aio_nav_node"}}

    def engine(self, **readiness):
        self.data["readiness"].update(readiness)
        self.cfg = Config(self.data, None)
        self.store, self.nav, self.clock, self.events = StateStore(), FakeNav(), Clock(), Events()
        self.store.set("services", {"aio_nav": {"label": "AIO NAV", "state": "running", "pids": [4242],
                                                "unit": "aio-nav.service",
                                                "unit_state": {"active": "inactive", "file_state": "disabled"}}})
        self.gnss("fixed")
        return HealthEngine(self.cfg, self.store, self.nav, self.events, clock=self.clock)

    def gnss(self, quality):
        self.store.set("gnss", {"available": True, "quality": quality, "sigma_h_m": 0.05})

    def run_for(self, eng, seconds, step=0.2):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            eng.step()
        return self.store.get("readiness")

    def aligned(self):
        self.nav.latest, self.nav.age = dict(ALIGNED), 0.01

    def test_default_config_reports_ready_like_before(self):
        eng = self.engine()
        self.aligned()
        r = self.run_for(eng, 6)
        product = self.store.get("product")
        self.assertEqual((r["state"], product["health"]["state"]), ("READY", "READY"))
        self.assertEqual(r["session_id"], eng.session_id)
        self.assertEqual(product["readiness"]["state"], "READY")
        self.assertEqual(r["nav"]["flags_c"], 0xE8)

    def test_required_rtk_float_with_stable_time_then_degraded(self):
        eng = self.engine(require=["gnss_quality"], gnss_min_quality="float", gnss_stable_s=3.0)
        self.aligned()
        self.gnss("spp")
        r = self.run_for(eng, 6)
        self.assertEqual((r["state"], r["requested_action"]), ("REFINING", "KEEP_DRIVING"))
        self.assertEqual(self.store.get("product")["health"]["state"], "INITIALIZING")   # page agrees
        self.gnss("unrated")                                         # a good unrated fix counts as float
        r = self.run_for(eng, 2)
        self.assertEqual(r["state"], "REFINING")                     # not yet 3 s stable
        r = self.run_for(eng, 2)
        self.assertEqual(r["state"], "READY")
        self.gnss("spp")                                             # lost after READY
        r = self.run_for(eng, 2.0)
        self.assertEqual(r["state"], "READY")                        # shorter than gnss_loss_s (3 s)
        r = self.run_for(eng, 1.6)
        self.assertEqual((r["state"], r["requested_action"]), ("DEGRADED", "SAFE_STOP"))
        self.assertEqual(self.store.get("product")["health"]["state"], "DEGRADED")
        self.new_process()                                           # a new AIO NAV run starts the flow again
        r = self.run_for(eng, 1)
        self.assertEqual(r["state"], "REFINING")

    def new_process(self, pid=5000):
        svc = self.store.get("services")
        svc["aio_nav"].update(pids=[pid], state="running")
        self.store.set("services", svc)

    def stop_process(self):
        svc = self.store.get("services")
        svc["aio_nav"].update(pids=[], state="stopped")
        self.store.set("services", svc)
        self.nav.age = None

    def test_crash_after_ready_asks_for_a_safe_stop(self):
        """Regression: the latch was per NAV session, and the session is cleared the moment AIO
        NAV disappears, so a crash while driving was reported as 'starting'."""
        from aio_system_dashboard.actions import process_control
        eng = self.engine()
        self.aligned()
        process_control.set_wanted(self.cfg, "aio_nav", True)
        self.clock.t += 100                                          # the start request is long past
        self.assertEqual(self.run_for(eng, 4)["state"], "READY")
        self.stop_process()                                          # it dies; nobody pressed Stop
        r = self.run_for(eng, 4)
        self.assertEqual((r["state"], r["requested_action"]), ("FAULT", "SAFE_STOP"))
        self.assertEqual(r["reasons"], ["AIO NAV stopped after it was ready"])
        self.assertEqual(self.store.get("product")["health"]["state"], "FAULT")
        self.new_process()                                           # a new process: alignment again
        self.nav.latest, self.nav.age = {"alignment": False}, 0.01
        r = self.run_for(eng, 4)
        self.assertEqual((r["state"], r["requested_action"]), ("KEEP_STILL", "HOLD_STILL"))

    def test_a_power_off_while_running_is_no_crash_at_the_next_power_on(self):
        """Seen on the device (2026-10-21): AIO NAV ran at power-off, so it was still "wanted" at
        the next power-on: the pages said FAULT "AIO NAV stopped unexpectedly" until the automatic
        start, and the vehicle got FAULT for a second when it began."""
        from aio_system_dashboard.actions import process_control
        eng = self.engine()
        process_control.set_wanted(self.cfg, "aio_nav", True)          # running at power-off
        self.stop_process()
        eng.autostart_status = lambda: {"enabled": True, "state": "waiting", "hold": False,
                                        "ready_this_boot": False, "detail": "Waiting for IMU data"}
        self.clock.t += 100
        with mock.patch.object(process_control, "_boot_id", lambda: "the-next-boot"):
            r = self.run_for(eng, 4)
            self.assertEqual((eng.product.level, r["state"]), ("STOPPED", "CHECKING"))
            self.assertNotIn(("health", "AIO NAV state FAULT"), self.events.items)
        with mock.patch.object(process_control, "wanted_age_s", lambda _c, _k: 100.0):
            self.run_for(eng, 4)                                        # the same boot: a crash
        self.assertEqual(eng.product.level, "FAULT")

    def test_crash_after_ready_survives_a_dashboard_restart(self):
        """Seen on the device: after a dashboard restart the per-process latch was gone and the
        vehicle was told SERVICE_REQUIRED ("press Start") instead of SAFE_STOP."""
        from aio_system_dashboard.actions import process_control
        eng = self.engine()
        process_control.set_wanted(self.cfg, "aio_nav", True)
        self.stop_process()                                          # crashed before this restart
        auto = {"enabled": True, "state": "ready_this_boot", "hold": False, "ready_this_boot": True}
        eng.autostart_status = lambda: auto
        self.clock.t += 100
        r = self.run_for(eng, 4)
        self.assertEqual((r["state"], r["requested_action"]), ("FAULT", "SAFE_STOP"))
        auto["ready_this_boot"] = False                              # the next power-on
        r = self.run_for(eng, 4)
        self.assertNotEqual(r["requested_action"], "SAFE_STOP")
        auto["ready_this_boot"] = True
        self.new_process()                                           # an operator Start: a new run
        self.nav.latest, self.nav.age = {"alignment": False}, 0.01
        r = self.run_for(eng, 4)
        self.assertEqual((r["state"], r["requested_action"]), ("KEEP_STILL", "HOLD_STILL"))

    def test_imu_without_valid_time_is_not_ready(self):
        """The receiver had no GNSS time indoors: IMU data arrived at 100 Hz with a frozen 1980
        stamp. The imu condition (an automatic-start prerequisite) must not hold."""
        eng = self.engine()
        imu = {"name": "/openrtk330/imu/data_raw", "label": "INS IMU", "group": "nav", "state": "healthy",
               "rate_hz": 100.0, "age_s": 0.01, "time_ok": True}
        self.store.set("ros", {"available": True, "watched": [imu]})
        r = self.run_for(eng, 1)
        self.assertTrue(next(c for c in r["conditions"] if c["name"] == "imu")["ok"])
        imu.update(time_ok=False, time_why="time stamps say 1980-01-05 23:59:42 UTC, not a real time")
        r = self.run_for(eng, 1)
        c = next(c for c in r["conditions"] if c["name"] == "imu")
        self.assertFalse(c["ok"])
        self.assertIn("1980-01-05", c["reason"])
        self.assertIn("no time yet", c["reason"])
        imu.update(time_ok=None)                                     # unknown: the rate decides
        r = self.run_for(eng, 1)
        self.assertTrue(next(c for c in r["conditions"] if c["name"] == "imu")["ok"])

    def test_vehicle_still_from_imu_motion(self):
        eng = self.engine()
        imu = {"name": "/openrtk330/imu/data_raw", "label": "INS IMU", "group": "nav", "state": "healthy",
               "rate_hz": 100.0, "age_s": 0.01, "motion": {"gyro_max": 0.01, "accel_std": 0.03}}
        self.store.set("ros", {"available": True, "watched": [imu]})
        self.assertTrue(self.run_for(eng, 1)["motion"]["still"])            # parked
        imu["motion"] = {"gyro_max": 0.4, "accel_std": 2.1}                 # driving
        m = self.run_for(eng, 1)["motion"]
        self.assertEqual((m["still"], m["reason"]), (False, "moving: rotation 0.40 rad/s"))
        imu["motion"] = {"gyro_max": 0.01, "accel_std": 0.3}                # rough idle / bumps
        self.assertFalse(self.run_for(eng, 1)["motion"]["still"])
        imu.pop("motion")
        self.assertIsNone(self.run_for(eng, 1)["motion"]["still"])          # unknown is not still

    def test_vehicle_still_uses_measured_limits_and_gnss_speed(self):
        """Measured limits replace the settings; with a GNSS position the receiver's speed counts too
        (an IMU alone cannot tell standing from driving straight at a steady speed)."""
        eng = self.engine()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        eng.still_limits = still.StillLimits(eng.cfg, Path(tmp.name) / "still_limits.json")
        imu = {"name": "/openrtk330/imu/data_raw", "label": "INS IMU", "group": "nav", "state": "healthy",
               "rate_hz": 100.0, "age_s": 0.01, "motion": {"gyro_max": 0.01, "accel_std": 0.2}}
        self.store.set("ros", {"available": True, "watched": [imu]})
        self.assertFalse(self.run_for(eng, 1)["motion"]["still"])           # 0.2 > the 0.15 default
        eng.still_limits.apply({"gyro_rad_s": 0.05, "accel_std": 0.3, "speed_m_s": 0.3}, "measured")
        self.assertTrue(self.run_for(eng, 1)["motion"]["still"])            # this vehicle shakes more
        self.store.set("speed", {"available": True, "speed_max": 2.5, "frozen": False, "age_s": 0.3})
        self.store.set("gnss", {"quality": "none"})
        m = self.run_for(eng, 1)["motion"]
        self.assertTrue(m["still"])                                          # no GNSS position: not used
        self.assertFalse(m["speed_used"])
        self.store.set("gnss", {"quality": "spp"})
        m = self.run_for(eng, 1)["motion"]
        self.assertEqual((m["still"], m["reason"]), (False, "moving: GNSS speed 2.50 m/s"))
        self.store.set("speed", {"available": True, "speed_max": 2.5, "frozen": True, "age_s": 0.3})
        self.assertTrue(self.run_for(eng, 1)["motion"]["still"])            # frozen: not a measurement

    def test_new_run_is_not_degraded_while_the_process_list_lags(self):
        """Seen on the device: right after an automatic restart (READY earlier in this power-on) the
        first packets arrived before the process list showed AIO NAV, and the vehicle was told
        DEGRADED / "alignment lost after it was ready" for a second."""
        eng = self.engine()
        eng.autostart_status = lambda: {"enabled": True, "state": "running", "hold": False,
                                        "ready_this_boot": True}
        self.stop_process()                                  # list not updated yet ...
        self.nav.latest, self.nav.age = {"alignment": False}, 0.01   # ... but the new run sends
        r = self.run_for(eng, 3)
        self.assertEqual((r["state"], r["requested_action"]), ("KEEP_STILL", "HOLD_STILL"))

    def test_operator_stop_after_ready_is_maintenance(self):
        from aio_system_dashboard.actions import process_control
        eng = self.engine()
        self.aligned()
        self.run_for(eng, 4)
        process_control.set_wanted(self.cfg, "aio_nav", False)       # an operator pressed Stop
        self.stop_process()
        r = self.run_for(eng, 4)
        self.assertEqual((r["state"], r["requested_action"]), ("MAINTENANCE", "MAINTENANCE_HOLD"))
        self.assertNotEqual(r["inhibit_flags"], 0)

    def test_rtk_is_required_by_default_and_can_be_switched_off(self):
        from aio_system_dashboard.state.health import set_rtk_setting
        eng = self.engine()                                          # defaults: require_rtk true
        self.aligned()
        self.gnss("spp")
        r = self.run_for(eng, 6)
        self.assertEqual(r["state"], "REFINING")
        self.assertIn("GNSS_QUALITY_LOW", r["inhibit"])
        self.assertEqual((r["rtk_required"], r["rtk_source"]), (True, "config"))
        set_rtk_setting(self.cfg, False, "192.0.2.1")                # the Maintenance switch
        r = self.run_for(eng, 1)
        self.assertEqual(r["state"], "READY")
        self.assertEqual((r["rtk_required"], r["rtk_source"]), (False, "saved setting"))

    def test_startup_checklist_follows_the_sequence(self):
        self.data["autostart"]["start_services"] = ["drivers"]
        self.data["services"]["drivers"] = {"label": "Sensor drivers", "user_unit": "d.service"}
        eng = self.engine()
        self.nav.latest, self.nav.age = {"alignment": True}, 0.01
        self.gnss("spp")
        self.run_for(eng, 4)
        steps = {c["key"]: c for c in self.store.get("product")["checks"]}
        self.assertEqual(list(steps), ["sensors", "gnss", "nav", "alignment", "vehicle"])
        self.assertEqual((steps["sensors"]["level"], steps["sensors"]["label"]), ("warning", "Waiting"))
        self.assertIn({"k": "Sensor drivers", "v": "Starting"}, steps["sensors"]["rows"])
        self.assertEqual((steps["gnss"]["level"], steps["gnss"]["label"]), ("warning", "SPP"))
        self.assertIn({"k": "RTK required for Ready", "v": "Yes (RTK float or fix)"}, steps["gnss"]["rows"])
        self.assertEqual((steps["nav"]["level"], steps["nav"]["label"]), ("healthy", "Running"))
        self.assertEqual((steps["alignment"]["level"], steps["alignment"]["label"]), ("warning", "Step 2 of 3"))
        self.assertEqual(steps["vehicle"]["label"], "Ready signal off")       # no 0x05 sender here

    def test_time_in_state_is_for_the_state_shown(self):
        """Regression: "Degraded for 1:00" after a minute of Ready and a moment of Degraded."""
        eng = self.engine()
        self.aligned()
        self.run_for(eng, 60)
        self.gnss("spp")                                             # RTK lost: Degraded after 3 s
        self.run_for(eng, 4)
        h = self.store.get("product")["health"]
        self.assertEqual(h["state"], "DEGRADED")
        self.assertLess(h["since_s"], 2.0)

    def test_dso_only_required_when_vupt_is_on(self):
        eng = self.engine(require=["dso"])
        self.aligned()
        self.store.set("watchdog", {"dso": {"state": "waiting", "last_msg_age_s": None}})
        r = self.run_for(eng, 4)
        self.assertEqual(r["state"], "REFINING")                     # aio_nav.yaml has Odometry.vupt: true
        self.assertIn("DSO_NOT_READY", r["inhibit"])
        self.store.set("watchdog", {"dso": {"state": "ok", "last_msg_age_s": 0.1}})
        r = self.run_for(eng, 3)
        self.assertEqual(r["state"], "READY")
        (self.tmp / "aio_nav.yaml").write_text("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n")
        self.cfg._aio_nav = None                                     # VUPT off: DSO no longer matters
        self.store.set("watchdog", {"dso": {"state": "bad", "last_msg_age_s": 9.0}})
        r = self.run_for(eng, 2)
        self.assertEqual(r["state"], "READY")

    def test_duplicate_process_is_debounced_then_a_fault(self):
        eng = self.engine()
        self.aligned()
        self.run_for(eng, 4)
        svc = self.store.get("services")
        svc["aio_nav"].update(duplicate=True, pids=[4242, 4243])
        self.store.set("services", svc)
        r = self.run_for(eng, 1)
        self.assertEqual(r["state"], "READY")                        # a brief overlap is not a fault
        r = self.run_for(eng, 3)
        self.assertEqual(r["state"], "FAULT")
        self.assertIn("OWNERSHIP_CONFLICT", r["inhibit"])
        self.assertEqual(self.store.get("product")["health"]["state"], "FAULT")
        issues = [i["title"] for i in self.store.get("maint")["issues"]]
        self.assertIn("AIO NAV is running twice", issues)

    def test_boot_unit_conflict_warns_and_blocks_only_with_autostart(self):
        self.data["nav"]["allow_control"] = True
        eng = self.engine()
        self.aligned()
        svc = self.store.get("services")
        svc["aio_nav"]["unit_state"]["file_state"] = "enabled"
        self.store.set("services", svc)
        r = self.run_for(eng, 4)
        self.assertEqual(r["state"], "READY")                        # manual mode: a warning only
        self.assertTrue(any("starts AIO NAV at boot" in i["title"] for i in self.store.get("maint")["issues"]))
        eng.autostart_status = lambda: {"enabled": True, "state": "running"}
        r = self.run_for(eng, 1)
        self.assertEqual(r["state"], "FAULT")
        self.assertIn("OWNERSHIP_CONFLICT", r["inhibit"])


if __name__ == "__main__":
    unittest.main()
