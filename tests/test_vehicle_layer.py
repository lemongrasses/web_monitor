"""The vehicle output layer (aio_system_dashboard/vehicle/): its numbers, and its boundary with the core.

The core works by name; this layer turns a readiness snapshot into the numbers the vehicle reads
(0x05). The tables below are a contract with the vehicle: a change here is a protocol change.
"""

import copy
import itertools
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.state import readiness as rd
from aio_system_dashboard.state.health import HealthEngine
from aio_system_dashboard.state.store import StateStore
from aio_system_dashboard.vehicle import protocol as proto
from aio_system_dashboard.vehicle.output import VehicleOutput
from tests.test_readiness import ALIGNED, Clock, Events, FakeNav, ev

PACKAGE = Path(__file__).resolve().parent.parent / "aio_system_dashboard"


class ProtocolTest(unittest.TestCase):
    def test_numbers_are_the_published_ones(self):
        """docs/OPERATION.md 6.8 and the vehicle's aio-nav-receiver use these: append, never renumber."""
        self.assertEqual(proto.STATE_CODE, {
            "BOOTING": 0, "CHECKING": 1, "NAV_STARTING": 2, "KEEP_STILL": 3, "MOVE_STRAIGHT": 4,
            "REFINING": 5, "READY": 6, "DEGRADED": 7, "FAULT": 8, "MAINTENANCE": 9})
        self.assertEqual(proto.ACTION_CODE, {
            "NONE": 0, "HOLD_STILL": 1, "DRIVE_STRAIGHT": 2, "KEEP_DRIVING": 3, "SAFE_STOP": 4,
            "SERVICE_REQUIRED": 5, "MAINTENANCE_HOLD": 6})
        self.assertEqual(list(proto.HEALTH_BITS), [
            "DASHBOARD_OK", "CONFIG_OK", "IMU_STREAM_OK", "GNSS_STREAM_OK", "GNSS_QUALITY_OK",
            "NAV_PROCESS_OK", "NAV_OUTPUT_OK", "ALIGNMENT_OK", "HEADING_OK", "FINE_ALIGNMENT_OK",
            "DSO_OK", "EXTERNAL_ROUTE_OK", "RTK_CORRECTION_OK", "STORAGE_OK", "VEHICLE_ACK_OK",
            "TIME_SYNC_OK", "NO_DUPLICATE_PROCESS", "WIFI_OK"])
        self.assertEqual(list(proto.HEALTH_BITS.values()), list(range(18)))
        self.assertEqual(list(proto.INHIBIT_BITS), [
            "BOOT_IN_PROGRESS", "CONFIG_INVALID", "IMU_MISSING_OR_STALE", "GNSS_MISSING_OR_STALE",
            "GNSS_QUALITY_LOW", "NAV_NOT_RUNNING", "NAV_OUTPUT_STALE", "KEEP_STILL_REQUIRED",
            "MOVE_STRAIGHT_REQUIRED", "FINE_ALIGNMENT_REQUIRED", "DSO_NOT_READY", "EXTERNAL_ROUTE_DOWN",
            "RTK_CORRECTION_LOST", "STORAGE_NOT_WRITABLE", "MAINTENANCE_HOLD", "STARTUP_TIMEOUT",
            "OWNERSHIP_CONFLICT", "INTERNAL_FAULT"])
        self.assertEqual(list(proto.INHIBIT_BITS.values()), list(range(18)))
        self.assertEqual(proto.GNSS_QUALITY_CODE, {"unknown": 0, "none": 1, "spp": 2, "rtk_float": 3, "rtk_fix": 4})

    def test_every_name_of_the_core_has_a_number(self):
        """A state, action or reason added to the core cannot reach the vehicle without one."""
        self.assertEqual(set(rd.STATES), set(proto.STATE_CODE))
        self.assertEqual(set(rd.ACTIONS), set(proto.ACTION_CODE))
        self.assertEqual(set(rd.HEALTH), set(proto.HEALTH_BITS))
        self.assertEqual(set(rd.INHIBIT), set(proto.INHIBIT_BITS))
        for rank, name in enumerate(rd.GNSS_QUALITY_NAMES):
            self.assertEqual(proto.GNSS_QUALITY_CODE[name], rank)

    def test_masks_round_trip(self):
        r = ev(product_state="INITIALIZING", step={"key": "heading_valid"}, nav_fresh=True)
        w = proto.wire_fields(r)
        self.assertEqual(proto.names_of(w["inhibit_flags"], proto.INHIBIT_BITS), r["inhibit"])
        self.assertEqual(proto.names_of(w["health_flags"], proto.HEALTH_BITS), r["health"])

    def test_not_ready_always_has_an_inhibit_bit(self):
        """The vehicle may check only inhibit_flags == 0."""
        for st, run, fresh, flags, ever in itertools.product(
                ("UNKNOWN", "STOPPED", "STARTING", "INITIALIZING", "READY", "FAULT"),
                (True, False), (True, False), ({}, ALIGNED, {"alignment": True}), (True, False)):
            r = ev(product_state=st, nav_running=run, nav_fresh=fresh, nav_flags=flags, ever_ready=ever,
                   step={"key": "alignment"} if st == "INITIALIZING" else None)
            w = proto.wire_fields(r)
            self.assertEqual(w["inhibit_flags"] == 0, r["state"] == "READY", (st, run, fresh, flags, ever))
            self.assertEqual(w["state_code"], proto.STATE_CODE[r["state"]])


class BoundaryTest(unittest.TestCase):
    def test_the_core_never_imports_the_vehicle_layer(self):
        """Only the app's wiring (__main__.py) knows the layer, so it can be left out or moved."""
        pattern = re.compile(r"^\s*(from|import)\s+(\.+|aio_system_dashboard\.)vehicle\b", re.M)
        found = []
        for f in PACKAGE.rglob("*.py"):
            rel = f.relative_to(PACKAGE).as_posix()
            if rel.startswith("vehicle/") or rel == "__main__.py":
                continue
            if pattern.search(f.read_text(encoding="utf-8")):
                found.append(rel)
        self.assertEqual(found, [])


class EngineWithVehicleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        cfg_yaml = self.tmp / "aio_nav.yaml"
        cfg_yaml.write_text("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n"
                            "    output_udp: '192.0.2.10:9000'\n")
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: self.tmp / "state" / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: self.tmp / "state" / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["aio_nav_config"] = str(cfg_yaml)
        self.data["services"] = {"aio_nav": {"label": "AIO NAV", "unit": "aio-nav.service",
                                             "process_pattern": "x/aio_nav_node"}}
        self.data["status_udp"]["enabled"] = False          # no packets leave the test
        self.cfg = Config(self.data, None)
        self.store, self.nav, self.clock = StateStore(), FakeNav(), Clock()
        self.store.set("services", {"aio_nav": {"label": "AIO NAV", "state": "running", "pids": [4242],
                                                "unit": "aio-nav.service",
                                                "unit_state": {"active": "inactive", "file_state": "disabled"}}})
        self.store.set("gnss", {"available": True, "quality": "fixed", "sigma_h_m": 0.05})
        self.nav.latest, self.nav.age = dict(ALIGNED), 0.01
        self.eng = HealthEngine(self.cfg, self.store, self.nav, Events(), clock=self.clock)

    def run_for(self, seconds, step=0.2):
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            self.eng.step()
        return self.store.get("readiness")

    def attach(self):
        vo = VehicleOutput(self.cfg, self.store, clock=self.clock)
        self.eng.snapshot_listeners.append(vo.on_snapshot)
        self.eng.extra_steps.append(vo.startup_step)
        return vo

    def test_without_the_layer_the_core_runs_and_has_no_numbers(self):
        r = self.run_for(6)
        self.assertEqual(r["state"], "READY")
        for k in ("state_code", "inhibit_flags", "health_flags", "session_id", "sequence"):
            self.assertNotIn(k, r)
        steps = [c["key"] for c in self.store.get("product")["checks"]]
        self.assertEqual(steps, ["sensors", "gnss", "nav", "alignment", "recording", "storage"])

    def test_the_layer_adds_the_numbers_the_vehicle_gets(self):
        vo = self.attach()
        r = self.run_for(6)
        self.assertEqual((r["state"], r["state_code"], r["inhibit_flags"]), ("READY", 6, 0))
        self.assertEqual((r["session_id"], r["gnss_quality"], r["gnss_quality_code"]), (vo.session_id, "rtk_fix", 4))
        self.assertEqual(r["nav"]["flags_c"], 0xE8)
        seq = r["sequence"]
        self.assertEqual(self.run_for(1)["sequence"], seq + 5)            # one per health step
        self.assertEqual(self.store.get("product")["readiness"]["state_code"], 6)

    def test_the_layer_adds_the_vehicle_link_step(self):
        self.attach()
        self.run_for(2)
        steps = {c["key"]: c for c in self.store.get("product")["checks"]}
        self.assertEqual(list(steps), ["sensors", "gnss", "nav", "alignment", "recording", "storage", "vehicle"])
        self.assertEqual(steps["vehicle"]["label"], "Ready signal off")

    def test_no_vehicle_link_step_where_no_vehicle_is_connected(self):
        from aio_system_dashboard.state.environment import set_override
        self.attach()
        set_override(self.cfg, "vehicle", False, "test")
        self.run_for(2)
        self.assertNotIn("vehicle", [c["key"] for c in self.store.get("product")["checks"]])

    def test_a_failing_layer_does_not_stop_the_health_step(self):
        def broken(*_a):
            raise RuntimeError("boom")
        self.eng.snapshot_listeners.append(broken)
        self.eng.extra_steps.append(broken)
        with self.assertLogs("aio_system_dashboard.state.health", "ERROR"):
            r = self.run_for(6)
        self.assertEqual(r["state"], "READY")
        self.assertEqual(len(self.store.get("product")["checks"]), 6)


if __name__ == "__main__":
    unittest.main()
