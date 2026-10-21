"""Copies of the NAV and 0x05 packets for the devices on the hotspot (collectors/hotspot_relay.py)."""

import copy
import tempfile
import unittest
from pathlib import Path

from aio_system_dashboard.collectors import hotspot_relay as hr
from aio_system_dashboard.config import DEFAULTS, Config

IW_DEV_AP = """phy#0
\tInterface p2p-dev-wlP1p1s0
\t\twdev 0x2
\t\ttype P2P-device
\tInterface wlP1p1s0
\t\tifindex 5
\t\tssid AIO-NAV
\t\ttype AP
\t\tchannel 1 (2412 MHz), width: 20 MHz, center1: 2412 MHz
"""
IW_DEV_CLIENT = IW_DEV_AP.replace("type AP", "type managed")
STATIONS = "Station a4:f9:21:00:00:01 (on wlP1p1s0)\n\tsignal:  \t-48 dBm\nStation 02:00:00:00:00:02 (on wlP1p1s0)\n"
NEIGH = ("10.42.0.186 lladdr a4:f9:21:00:00:01 REACHABLE\n"
         "10.42.0.77 lladdr 02:00:00:00:00:02 STALE\n"
         "10.42.0.99 lladdr 02:00:00:00:00:09 STALE\n"          # left: not associated any more
         "10.42.0.50  FAILED\n"
         "192.0.2.5 lladdr 02:00:00:00:00:02 REACHABLE\n")      # not the hotspot's network


class Runner:
    def __init__(self, iw_dev=IW_DEV_AP):
        self.iw_dev = iw_dev

    def __call__(self, argv, timeout=3.0):
        if argv == ["iw", "dev"]:
            return 0, self.iw_dev
        if argv[:2] == ["iw", "dev"] and argv[-2:] == ["station", "dump"]:
            return 0, STATIONS
        if argv[:3] == ["ip", "-4", "neigh"]:
            return 0, NEIGH
        return 1, ""


class Sock:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        pass


class RelayTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "hotspot_relay.json"

    def make(self, runner=None, offered=True):
        r = hr.HotspotRelay(Config(copy.deepcopy(DEFAULTS), None), self.state, runner=runner or Runner(),
                            offered=lambda: offered)
        r._sock = Sock()
        return r

    def test_copies_go_to_each_device_on_the_hotspot_only(self):
        r = self.make()
        r.refresh()
        self.assertEqual(r.targets, [("10.42.0.186", 9000), ("10.42.0.77", 9000)])
        r.send(b"nav")
        self.assertEqual(r._sock.sent, [(b"nav", ("10.42.0.186", 9000)), (b"nav", ("10.42.0.77", 9000))])
        st = r.status()
        self.assertEqual((st["active"], st["targets"], st["sent"]), (True, ["10.42.0.186", "10.42.0.77"], 2))

    def test_nothing_while_the_wifi_joins_a_network_or_without_the_hotspot(self):
        for r in (self.make(Runner(IW_DEV_CLIENT)), self.make(offered=False)):
            r.refresh()
            r.send(b"nav")
            self.assertEqual((r.active, r.targets, r._sock.sent), (False, [], []))

    def test_the_switch_is_kept(self):
        r = self.make()
        r.refresh()
        r.set_enabled(False, "test")
        r.send(b"nav")
        self.assertEqual(r._sock.sent, [])
        self.assertFalse(self.make().enabled)                           # after a restart too
        r.set_enabled(True)
        self.assertTrue(self.make().enabled)

    def test_nav_and_status_packets_are_handed_to_the_relay(self):
        from aio_system_dashboard.collectors.nav_udp import NavUdpCollector
        from aio_system_dashboard.collectors.status_udp import StatusUdpPublisher
        got = []
        nav = NavUdpCollector("127.0.0.1:9000", DEFAULTS["nav"]["trajectory"], relay=got.append)
        nav.handle_datagram(b"\x00garbage", 1.0)                      # copied as received
        self.assertEqual(got, [b"\x00garbage"])
        data = copy.deepcopy(DEFAULTS)
        data["status_udp"]["destination"] = "192.0.2.10:9000"
        pub = StatusUdpPublisher(Config(data, None), type("S", (), {"set": lambda *a: None})(), clock=lambda: 5.0)
        pub._sock = Sock()
        pub.relay = got.append
        from tests.test_status_packet import snap
        pub.publish(snap(), now=0.0)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1], pub._sock.sent[0][0])                  # the same 0x05 packet as the vehicle's


if __name__ == "__main__":
    unittest.main()
