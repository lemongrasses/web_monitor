"""0x05 status packet: codec (golden vector), publisher, and the vehicle-side reference gate."""

import copy
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.collectors.status_udp import StatusUdpPublisher
from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.nav import status_packet as sp
from aio_system_dashboard.nav.decoder import encode_nav_packet, flags_to_int
from aio_system_dashboard.nav.vehicle_gate import VehicleGate
from aio_system_dashboard.state.store import StateStore

GOLDEN_FIELDS = dict(state=6, requested_action=0, gnss_quality=3, session_id=0x12345678, sequence=482,
                     uptime_ms=123456789, health_flags=0x00010FFF, inhibit_flags=0, nav_flags=0xE8,
                     nav_rate_dhz=1000, nav_age_ms=12)
# Shared with the vehicle team (docs/OPERATION.md): any implementation must produce these bytes.
GOLDEN_HEX = "55aa0524000106000378563412e201000015cd5b0700000000ff0f010000000000e800e8030c0000005c"


class CodecTest(unittest.TestCase):
    def test_golden_vector(self):
        pkt = sp.encode(GOLDEN_FIELDS)
        self.assertEqual(len(pkt), 42)
        self.assertEqual(pkt.hex(), GOLDEN_HEX)
        d = sp.decode(bytes.fromhex(GOLDEN_HEX))
        for k, v in GOLDEN_FIELDS.items():
            self.assertEqual(d[k], v, k)

    def test_manuals_publish_the_same_golden_vector(self):
        root = Path(__file__).resolve().parent.parent / "docs"
        for name in ("OPERATION.md", "OPERATION.zh-TW.md"):
            self.assertIn(GOLDEN_HEX, (root / name).read_text(encoding="utf-8"), name)

    def test_rejects_corrupt_short_and_unknown(self):
        good = bytes.fromhex(GOLDEN_HEX)
        bad_sum = bytearray(good)
        bad_sum[-1] ^= 1
        bad_version = bytearray(good)
        bad_version[5] = 2
        bad_version[-1] = sum(bad_version[2:-1]) & 0xFF
        for data in (bytes(bad_sum), good[:-1], bytes(bad_version), b"\x55\xaa\x04" + good[3:]):
            self.assertIsNone(sp.decode(data))

    def test_clamps_and_missing_age(self):
        d = sp.decode(sp.encode(dict(GOLDEN_FIELDS, nav_age_ms=None, nav_rate_dhz=10 ** 9, sequence=2 ** 40)))
        self.assertEqual((d["nav_age_ms"], d["nav_rate_dhz"], d["sequence"]), (0xFFFF, 0xFFFF, 0xFFFFFFFF))

    def test_other_message_id(self):
        pkt = sp.encode(GOLDEN_FIELDS, message_id=0x07)
        self.assertIsNone(sp.decode(pkt))
        self.assertEqual(sp.decode(pkt, message_id=0x07)["state"], 6)


def snap(state="READY", code=6, inhibit=0, session=7):
    return {"state": state, "state_code": code, "requested_action": "NONE", "requested_action_code": 0,
            "gnss_quality_code": 4, "session_id": session, "uptime_ms": 1000, "health_flags": 1,
            "inhibit_flags": inhibit, "nav": {"flags_c": 0xE8, "rate_hz": 100.0, "age_ms": 5}}


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(0.5)
        self.addCleanup(self.rx.close)
        self.port = self.rx.getsockname()[1]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        y = Path(tmp.name) / "aio_nav.yaml"
        # "auto" sends 0x05 where 0x04 goes: this receiver
        y.write_text(f"aio_nav_node:\n  ros__parameters:\n    output_udp: '127.0.0.1:{self.port}'\n")
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["aio_nav_config"] = str(y)
        self.data["status_udp"]["enabled"] = True

    def pub(self, **status_udp):
        self.data["status_udp"].update(status_udp)
        with mock.patch.object(Config, "state_file", lambda _s, n: Path("/nonexistent") / n):
            p = StatusUdpPublisher(Config(self.data, None), StateStore())
        self.addCleanup(p.close)
        return p

    def recv(self):
        return sp.decode(self.rx.recvfrom(256)[0])

    def test_auto_destination_rate_and_send_on_change(self):
        p = self.pub(rate_hz=2.0)
        p.publish(snap(), now=0.0)
        self.assertEqual(self.recv()["state"], 6)
        p.publish(snap(), now=0.2)                        # not due, nothing changed: no packet
        p.publish(snap("DEGRADED", 7, inhibit=1 << 4), now=0.3)   # a change goes out at once
        d = self.recv()
        self.assertEqual((d["state"], d["inhibit_flags"], d["sequence"]), (7, 1 << 4, 2))
        p.publish(snap("DEGRADED", 7, inhibit=1 << 4), now=0.81)  # due again after 1/rate
        self.assertEqual(self.recv()["sequence"], 3)
        self.rx.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            self.rx.recvfrom(256)
        self.assertEqual(p.status()["sent"], 3)

    def test_auto_is_the_nav_address_and_port(self):
        addr, why = self.pub().destination()
        self.assertEqual(addr, ("127.0.0.1", self.port))

    def test_refuses_the_dashboards_own_listener(self):
        p = self.pub(destination="127.0.0.1:9000")
        addr, why = p.destination()
        self.assertIsNone(addr)
        self.assertIn("own NAV listener", why)
        p.publish(snap(), now=0.0)
        self.assertEqual((p.sent, p.errors), (0, 1))

    def test_host_names_are_refused(self):
        p = self.pub(destination="vehicle.local:9000")
        addr, why = p.destination()
        self.assertIsNone(addr)
        self.assertIn("IPv4", why)

    def test_send_error_is_counted_not_raised(self):
        p = self.pub(destination=f"127.0.0.1:{self.port}")
        p._sock = mock.Mock(sendto=mock.Mock(side_effect=OSError(101, "Network is unreachable")))
        p.publish(snap(), now=0.0)
        self.assertEqual(p.errors, 1)
        self.assertIn("unreachable", p.status()["last_error"])

    def test_disabled_sends_nothing(self):
        p = self.pub(enabled=False)
        p.publish(snap(), now=0.0)
        self.rx.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            self.rx.recvfrom(256)


def status_pkt(seq, state=6, inhibit=0, session=1):
    return sp.encode(dict(GOLDEN_FIELDS, state=state, inhibit_flags=inhibit, session_id=session, sequence=seq))


def nav_pkt(t, aligned=True, lat=22.99):
    flags = flags_to_int(alignment=aligned, heading_valid=aligned, fine_alignment=aligned)
    return encode_nav_packet(time_s=t, latitude=lat, longitude=120.2, flags_c=flags)


class VehicleGateTest(unittest.TestCase):
    """The receiver rules of the plan's section 7.4, scenario by scenario (section 17.2)."""

    def stream(self, g, t0, t1, ready=True, nav=True, aligned=True, session=1, seq0=1):
        seq, t = seq0, t0
        while t < t1:
            if nav:
                g.feed(nav_pkt(t, aligned), t)
            if abs((t * 2) - round(t * 2)) < 1e-9:      # status at 2 Hz
                g.feed(status_pkt(seq, 6 if ready else 4, 0 if ready else 1 << 8, session), t)
                seq += 1
            t = round(t + 0.01, 2)
        return seq

    def test_nav_before_ready_is_blocked(self):
        g = VehicleGate()
        self.stream(g, 0.0, 10.0, ready=False, aligned=False)
        self.assertFalse(g.allowed(10.0)[0])

    def test_ready_must_be_stable_and_flags_valid(self):
        g = VehicleGate()
        self.stream(g, 0.0, 1.5)
        self.assertEqual(g.allowed(1.5), (False, "READY not yet stable"))
        self.stream(g, 1.5, 3.0, seq0=4)
        self.assertEqual(g.allowed(3.0), (True, "ok"))

    def test_status_timeout_and_nav_timeout(self):
        g = VehicleGate()
        self.stream(g, 0.0, 3.0)
        self.assertTrue(g.allowed(3.0)[0])
        self.assertFalse(g.allowed(3.6)[0])                # 0x04 stopped: too old after 0.5 s
        g2 = VehicleGate()
        self.stream(g2, 0.0, 3.0)
        t = 3.0
        while t < 6.5:                                     # 0x05 stops, 0x04 continues
            g2.feed(nav_pkt(t), t)
            t = round(t + 0.01, 2)
        self.assertEqual(g2.allowed(6.5), (False, "no current status packet"))

    def test_session_change_clears_at_once(self):
        g = VehicleGate()
        seq = self.stream(g, 0.0, 3.0)
        self.assertTrue(g.allowed(3.0)[0])
        g.feed(status_pkt(seq, session=2), 3.0)            # dashboard restarted
        self.assertEqual(g.allowed(3.0), (False, "READY not yet stable"))

    def test_corrupt_status_and_nan_block(self):
        g = VehicleGate()
        seq = self.stream(g, 0.0, 3.0)
        bad = bytearray(status_pkt(seq))
        bad[-1] ^= 0xFF
        g.feed(bytes(bad), 3.0)
        self.assertFalse(g.allowed(3.0)[0])
        g2 = VehicleGate()
        self.stream(g2, 0.0, 3.0)
        g2.feed(nav_pkt(3.0, lat=float("nan")), 3.0)
        self.assertEqual(g2.allowed(3.0), (False, "navigation data not finite"))

    def test_one_lost_status_packet_does_not_block(self):
        g = VehicleGate()
        self.stream(g, 0.0, 3.0)
        g.feed(status_pkt(20), 3.0)                         # sequence jumped: packets lost
        self.assertTrue(g.allowed(3.0)[0])
        self.assertGreater(g.lost, 0)

    def test_forged_ready_ahead_in_the_real_session_is_never_stable(self):
        """Seen on the device: a forged READY with the real session id and a sequence 60 ahead was
        USABLE for 13 s while the real status said REFINING, and afterwards the real packets were
        ignored as old for minutes. Now two senders in one session clear the authorisation."""
        g = VehicleGate()
        seq, t, forged, usable = 1, 0.0, 61, False
        while t < 15.0:
            g.feed(nav_pkt(t), t)                           # forged aligned 0x04 at 100 Hz
            if abs(t * 20 - round(t * 20)) < 1e-9:          # forged READY at 20 Hz
                g.feed(status_pkt(forged), t)
                forged += 1
            if abs(t * 2 - round(t * 2)) < 1e-9:            # the real dashboard: not ready, 2 Hz
                g.feed(status_pkt(seq, state=5, inhibit=1 << 9), t)
                seq += 1
            usable |= g.allowed(t)[0]
            t = round(t + 0.01, 2)
        self.assertFalse(usable)
        self.assertGreater(g.conflicts, 0)
        g.feed(status_pkt(seq, state=5, inhibit=1 << 9), 15.5)   # the forger stopped
        self.assertEqual(g.status["state"], 5)              # the real stream counts again at once

    def test_a_few_late_packets_are_only_ignored(self):
        g = VehicleGate()
        seq = self.stream(g, 0.0, 3.0)
        g.feed(status_pkt(seq - 3), 3.0)                    # late, within the window
        self.assertTrue(g.allowed(3.0)[0])
        self.assertEqual((g.reordered, g.conflicts), (1, 0))

    def test_other_senders_can_be_refused(self):
        g = VehicleGate(allowed_from="192.0.2.10")
        self.assertIsNone(g.feed(status_pkt(1), 0.0, "192.0.2.66"))
        self.assertEqual(g.feed(status_pkt(1), 0.0, "192.0.2.10"), "status")
        self.assertEqual(g.foreign, 1)

    def test_status_and_nav_on_the_same_port_are_told_apart(self):
        g = VehicleGate()
        self.assertEqual(g.feed(status_pkt(1), 0.0), "status")
        self.assertEqual(g.feed(nav_pkt(0.0), 0.0), "nav")
        self.assertIsNone(g.feed(b"\x00" * 10, 0.0))


if __name__ == "__main__":
    unittest.main()
