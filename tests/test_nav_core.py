import os
import struct
import tempfile
import unittest

from aio_system_dashboard.data_access.files import DataAccessError, DataRoots
from aio_system_dashboard.nav.decoder import (
    NAV_PACKET_SIZE, encode_nav_packet, flags_to_int, parse_nav_packet, reverse_bits8)
from aio_system_dashboard.nav.trajectory import TrajectoryBuffer
from aio_system_dashboard.state.health import (
    INITIALIZING, PRODUCT_FAULT, PRODUCT_UNKNOWN, READY, raw_product_state, raw_udp_level)
from aio_system_dashboard.state.indicator import (
    FAULT, HEALTHY, UNKNOWN, WARNING, DebouncedStatus)

NCFG = {"stale_warn_s": 0.5, "stale_fault_s": 2.0, "low_rate_ratio": 0.8,
        "ready_requires": ["alignment", "heading_valid"]}


class DecoderTest(unittest.TestCase):
    def test_packet_size_matches_cpp_struct(self):
        # sync(2) + id + len(2) + payload(8+16+28*4+2+12 = 150) + checksum
        self.assertEqual(NAV_PACKET_SIZE, 156)

    def test_round_trip(self):
        pkt = encode_nav_packet(time_s=12345.678901, latitude=22.9969, longitude=120.2218,
                                height=35.5, velocity_north=1.0, velocity_east=-2.0,
                                velocity_up=0.5, heading=278.4,
                                flags_c=flags_to_int(alignment=True, gnss=True))
        d = parse_nav_packet(pkt)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d["time_s"], 12345.678901, places=6)
        self.assertAlmostEqual(d["latitude"], 22.9969, places=9)
        self.assertAlmostEqual(d["velocity_up"], 0.5, places=5)
        self.assertAlmostEqual(d["heading"], 278.4, places=4)
        self.assertTrue(d["alignment"] and d["gnss"])
        self.assertFalse(d["zupt"] or d["vupt"])

    def test_wire_bit3_is_vupt(self):
        # C bit 4 (VUPT) is bit 3 on the wire after aio_nav_node's reverse_bits8.
        self.assertEqual(reverse_bits8(1 << 4), 1 << 3)
        pkt = bytearray(encode_nav_packet(time_s=1, latitude=1, longitude=1))
        flags_off = 5 + struct.calcsize("<Qdd28f")
        pkt[flags_off] = 1 << 3
        pkt[-1] = sum(pkt[2:-1]) & 0xFF
        d = parse_nav_packet(bytes(pkt))
        self.assertTrue(d["vupt"])
        self.assertNotIn("imu_valid", d)
        self.assertEqual([k for k in ("zupt", "zihr", "nhc", "gnss", "alignment",
                                      "heading_valid", "fine_alignment") if d[k]], [])

    def test_velocity_is_up(self):
        d = parse_nav_packet(encode_nav_packet(time_s=1, latitude=1, longitude=1, velocity_up=3.0))
        self.assertIn("velocity_up", d)
        self.assertNotIn("velocity_down", d)
        self.assertAlmostEqual(d["velocity_up"], 3.0)

    def test_rejects_invalid(self):
        good = encode_nav_packet(time_s=1, latitude=1, longitude=1)
        self.assertIsNone(parse_nav_packet(good[:-2]))
        self.assertIsNone(parse_nav_packet(b"\x00\x00" + good[2:]))
        bad_sum = bytearray(good)
        bad_sum[-1] ^= 0xFF
        self.assertIsNone(parse_nav_packet(bytes(bad_sum)))
        bad_id = bytearray(good)
        bad_id[2] = 0x05
        self.assertIsNone(parse_nav_packet(bytes(bad_id)))
        bad_len = bytearray(good)
        bad_len[3] = 0x10
        self.assertIsNone(parse_nav_packet(bytes(bad_len)))


class TrajectoryTest(unittest.TestCase):
    def test_decimation_and_promotion(self):
        tb = TrajectoryBuffer(recent_window_s=60, recent_hz=10, older_hz=1)
        for i in range(100 * 120):  # 120 s at 100 Hz
            tb.add(i / 100.0, 22.0 + i * 1e-7, 120.0)
        snap = tb.snapshot(None, 0)
        self.assertTrue(snap["reset"])
        self.assertTrue(590 <= len(snap["recent"]) <= 602)
        self.assertTrue(55 <= len(snap["older"]) <= 61)
        # incremental: nothing new since cursor
        again = tb.snapshot(snap["session"], snap["cursor"])
        self.assertFalse(again["reset"])
        self.assertEqual(again["older"], [])

    def test_reset_starts_new_session(self):
        tb = TrajectoryBuffer()
        tb.add(0, 22.0, 120.0)
        old = tb.session
        tb.reset()
        snap = tb.snapshot(old, 5)
        self.assertNotEqual(snap["session"], old)
        self.assertTrue(snap["reset"])
        self.assertEqual(snap["recent"], [])

    def test_ignores_invalid_position(self):
        tb = TrajectoryBuffer()
        tb.add(0, 0.0, 0.0)
        tb.add(1, 95.0, 10.0)
        self.assertEqual(tb.snapshot(None)["recent"], [])


class IndicatorTest(unittest.TestCase):
    def test_hysteresis(self):
        ind = DebouncedStatus(raise_hold_s=1.0, clear_hold_s=2.0)
        self.assertEqual(ind.update(HEALTHY, 0.0), HEALTHY)   # leaving UNKNOWN is immediate
        self.assertEqual(ind.update(FAULT, 0.1), HEALTHY)     # single bad sample ignored
        self.assertEqual(ind.update(HEALTHY, 0.2), HEALTHY)
        self.assertEqual(ind.update(FAULT, 1.0), HEALTHY)
        self.assertEqual(ind.update(FAULT, 2.1), FAULT)       # persisted > raise hold
        self.assertEqual(ind.update(HEALTHY, 3.0), FAULT)     # recovery must be stable
        self.assertEqual(ind.update(HEALTHY, 4.5), FAULT)
        self.assertEqual(ind.update(HEALTHY, 5.1), HEALTHY)

    def test_flapping_recovery_restarts_hold(self):
        ind = DebouncedStatus(raise_hold_s=0.0, clear_hold_s=2.0, initial=WARNING)
        ind.update(HEALTHY, 0.0)
        ind.update(WARNING, 1.5)
        self.assertEqual(ind.update(HEALTHY, 2.5), WARNING)
        self.assertEqual(ind.update(HEALTHY, 4.6), HEALTHY)


def nav(age=0.01, rate=100.0, **flags):
    latest = {"alignment": True, "heading_valid": True}
    latest.update(flags)
    return {"listening": True, "bind_error": None, "age_s": age, "rate_hz": rate, "latest": latest}


class HealthTest(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(raw_product_state(nav(), "running", NCFG, False)[0], READY)

    def test_gnss_loss_stays_ready(self):
        self.assertEqual(raw_product_state(nav(gnss=False), "running", NCFG, False)[0], READY)

    def test_not_aligned_is_initializing(self):
        state, reasons = raw_product_state(nav(alignment=False), "running", NCFG, False)
        self.assertEqual(state, INITIALIZING)
        self.assertIn("Waiting for alignment", reasons)

    def test_stale_is_fault(self):
        self.assertEqual(raw_product_state(nav(age=2.5), "running", NCFG, False)[0], PRODUCT_FAULT)

    def test_process_down_is_fault(self):
        self.assertEqual(raw_product_state(nav(), "stopped", NCFG, False)[0], PRODUCT_FAULT)

    def test_no_packets(self):
        n = {"listening": True, "age_s": None, "latest": None}
        self.assertEqual(raw_product_state(n, None, NCFG, True)[0], PRODUCT_UNKNOWN)
        self.assertEqual(raw_product_state(n, None, NCFG, False)[0], PRODUCT_FAULT)

    def test_udp_levels(self):
        self.assertEqual(raw_udp_level(nav(), NCFG, 100.0, False)[1], "Streaming")
        self.assertEqual(raw_udp_level(nav(age=1.0), NCFG, 100.0, False)[0], WARNING)
        self.assertEqual(raw_udp_level(nav(age=3.0), NCFG, 100.0, False)[1], "Lost")
        self.assertEqual(raw_udp_level(nav(rate=50.0), NCFG, 100.0, False)[1], "Low rate")
        self.assertEqual(raw_udp_level({"age_s": None}, NCFG, 100.0, True)[0], UNKNOWN)


class DataAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.root = os.path.join(base, "data")
        os.makedirs(os.path.join(self.root, "run1"))
        with open(os.path.join(self.root, "run1", "fusion.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(base, "secret.txt"), "w") as f:
            f.write("s")
        os.symlink(os.path.join(base, "secret.txt"), os.path.join(self.root, "escape.txt"))
        self.roots = DataRoots([{"id": "logs", "name": "Logs", "path": self.root}])

    def tearDown(self):
        self.tmp.cleanup()

    def test_listing_and_download(self):
        lst = self.roots.listing("logs", "")
        self.assertEqual([e["name"] for e in lst["entries"]], ["run1"])  # escaping symlink hidden
        self.assertTrue(self.roots.file_for_download("logs", "run1/fusion.txt").is_file())

    def test_traversal_rejected(self):
        for rel in ("../secret.txt", "run1/../../secret.txt", "/etc/passwd", "escape.txt"):
            with self.assertRaises(DataAccessError, msg=rel):
                self.roots.file_for_download("logs", rel)
        with self.assertRaises(DataAccessError):
            self.roots.listing("other", "")


if __name__ == "__main__":
    unittest.main()


class NavStopClearsStateTest(unittest.TestCase):
    """AIO NAV always starts from scratch, so its last run must not stay on screen after it stops."""

    def collector(self):
        from aio_system_dashboard.collectors.nav_udp import NavUdpCollector
        resets = []
        c = NavUdpCollector("127.0.0.1:59999", {}, on_session_reset=lambda s, r: resets.append(r))
        pkt = encode_nav_packet(time_s=10.0, latitude=22.99, longitude=120.22, height=30.0,
                                velocity_north=1.0, velocity_east=0.0, velocity_up=0.0, heading=90.0,
                                flags_c=flags_to_int(alignment=True, heading_valid=True))
        c.handle_datagram(pkt, 100.0)
        return c, resets, pkt

    def test_clear_forgets_solution_trajectory_and_session(self):
        c, resets, pkt = self.collector()
        st = c.status(100.1)
        self.assertIsNotNone(st["latest"]); self.assertTrue(st["flag_age_s"])
        session = c.trajectory.session
        c.clear("AIO NAV stopped")
        st = c.status(101.0)
        self.assertIsNone(st["latest"]); self.assertIsNone(st["age_s"])
        self.assertEqual(st["flag_age_s"], {}); self.assertEqual(st["rate_hz"], 0)
        self.assertIsNone(st["session_started"])
        self.assertNotEqual(c.trajectory.session, session)
        self.assertEqual(c.trajectory.snapshot(None)["recent"], [])
        self.assertEqual(resets, ["AIO NAV stopped"])
        c.handle_datagram(pkt, 105.0)                    # the next run starts a fresh session clock
        self.assertIsNotNone(c.status(105.1)["session_started"])

    def test_health_clears_on_stop_only(self):
        from types import SimpleNamespace
        from aio_system_dashboard.state.health import HealthEngine
        calls = []
        nav = SimpleNamespace(clear=lambda r: calls.append(("clear", r)),
                              new_session=lambda r: calls.append(("new", r)))
        h = SimpleNamespace(cfg={"nav": {"service": "aio_nav"}}, nav=nav, _last_nav_pids=None)
        step = lambda pids: HealthEngine._check_nav_restart(h, {"aio_nav": {"pids": pids}})
        step([]); self.assertEqual(calls, [])                 # never ran: nothing to clear
        step([11]); step([11]); self.assertEqual(calls, [])  # running
        step([]);  self.assertEqual(calls, [("clear", "AIO NAV stopped")])
        step([]);  self.assertEqual(len(calls), 1)            # cleared once, not every tick
        step([12]); step([13])
        self.assertEqual(calls[-1], ("new", "aio_nav_node restarted"))
