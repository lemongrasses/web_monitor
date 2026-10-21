"""Copies of the NAV and 0x05 packets for the devices on the hotspot (collectors/hotspot_relay.py)."""

import copy
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard import nl80211 as nl
from aio_system_dashboard.collectors import hotspot_relay as hr
from aio_system_dashboard.config import DEFAULTS, Config

NEIGH = ("10.42.0.186 lladdr a4:f9:21:00:00:01 REACHABLE\n"
         "10.42.0.77 lladdr 02:00:00:00:00:02 STALE\n"
         "10.42.0.99 lladdr 02:00:00:00:00:09 STALE\n"          # left: not associated any more
         "10.42.0.50  FAILED\n"
         "192.0.2.5 lladdr 02:00:00:00:00:02 REACHABLE\n")      # not the hotspot's network
ON_THE_HOTSPOT = ("wlP1p1s0", {"a4:f9:21:00:00:01", "02:00:00:00:00:02"})


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=3.0):
        self.calls.append(argv)
        if argv[:3] == ["ip", "-4", "neigh"]:
            return 0, NEIGH
        return 1, ""


def msg(kind, seq, payload):
    return struct.pack("=IHHII", 16 + len(payload), kind, 0, seq, 0) + payload


def genl(cmd, *attrs):
    return struct.pack("=BBH", cmd, 1, 0) + b"".join(attrs)


class Kernel:
    """A netlink socket that answers like the kernel: the nl80211 family, the Wi-Fi interfaces
    (and a P2P device, which has no name) and the devices associated with each."""
    FAMILY = 24

    def __init__(self, ifaces, stations):
        self.ifaces, self.stations, self.pending, self.sent = ifaces, stations, [], []

    def send(self, data):
        size, family, flags, seq, _ = struct.unpack_from("=IHHII", data)
        cmd, a = data[16], nl.attrs(data[20:size])
        self.sent.append((family, cmd, flags))
        stale = msg(family, seq - 1, genl(cmd))                    # a late answer to an earlier request
        if family == nl.GENL_ID_CTRL:
            assert a[nl.CTRL_ATTR_FAMILY_NAME] == b"nl80211\0"
            self.pending.append(msg(family, seq, genl(1, nl.attr(nl.CTRL_ATTR_FAMILY_ID, struct.pack("=H", self.FAMILY)))))
            return
        if cmd == nl.CMD_GET_INTERFACE:
            parts = [msg(self.FAMILY, seq, genl(7, nl.attr(153, struct.pack("=Q", 2)), nl.attr(nl.ATTR_IFTYPE, struct.pack("=I", 10))))]
            parts += [msg(self.FAMILY, seq, genl(7, nl.attr(nl.ATTR_IFINDEX, struct.pack("=I", i)),
                                                 nl.attr(nl.ATTR_IFNAME, name.encode() + b"\0"),
                                                 nl.attr(nl.ATTR_IFTYPE, struct.pack("=I", kind))))
                      for i, (name, kind) in self.ifaces.items()]
        else:
            index = struct.unpack("=I", a[nl.ATTR_IFINDEX])[0]
            if index not in self.stations:
                self.pending.append(msg(nl.NLMSG_ERROR, seq, struct.pack("=i", -19) + data[:16]))   # ENODEV
                return
            parts = [msg(self.FAMILY, seq, genl(19, nl.attr(nl.ATTR_IFINDEX, struct.pack("=I", index)),
                                                nl.attr(nl.ATTR_MAC, bytes.fromhex(mac.replace(":", "")))))
                     for mac in self.stations[index]]
        self.pending += [stale + b"".join(parts[:1]), b"".join(parts[1:]), msg(nl.NLMSG_DONE, seq, b"\0" * 4)]

    def recv(self, size):
        return self.pending.pop(0)

    def close(self):
        pass


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

    def make(self, access_point=lambda: ON_THE_HOTSPOT, offered=True):
        self.runner = Runner()
        r = hr.HotspotRelay(Config(copy.deepcopy(DEFAULTS), None), self.state, runner=self.runner,
                            offered=lambda: offered, access_point=access_point)
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
        for r in (self.make(access_point=lambda: None), self.make(offered=False)):
            r.refresh()
            r.send(b"nav")
            self.assertEqual((r.active, r.targets, r._sock.sent), (False, [], []))

    def test_no_iw_needed(self):
        """A fresh system has no iw package: the devices come from the kernel, never from iw."""
        r = self.make()
        r.refresh()
        self.assertEqual(len(r.targets), 2)
        self.assertEqual(self.runner.calls, [["ip", "-4", "neigh", "show", "dev", "wlP1p1s0"]])

    def test_a_wifi_that_cannot_be_read_is_said_once(self):
        def broken():
            raise OSError(93, "Protocol not supported")
        r = self.make(access_point=broken)
        with self.assertLogs(hr.logger, "WARNING") as logs:
            r.refresh()
            r.refresh()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((r.active, r.targets), (False, []))
        self.assertIn("Protocol not supported", r.status()["last_error"])

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


class Nl80211Test(unittest.TestCase):
    """The kernel requests iw makes, without iw (nl80211.py)."""

    def kernel(self, stations=None):
        return Kernel({7: ("wlP1p1s0", nl.IFTYPE_AP), 9: ("wlan1", 2)},
                      {7: ["a4:f9:21:00:00:01", "02:00:00:00:00:02"], 9: []} if stations is None else stations)

    def test_interfaces_and_the_devices_on_the_access_point(self):
        k = self.kernel()
        with nl.Nl80211(sock=k) as n:
            self.assertEqual(n.family, Kernel.FAMILY)
            self.assertEqual(n.interfaces(), [{"name": "wlP1p1s0", "ifindex": 7, "type": nl.IFTYPE_AP},
                                              {"name": "wlan1", "ifindex": 9, "type": 2}])     # no P2P device
            self.assertEqual(n.stations(7), {"a4:f9:21:00:00:01", "02:00:00:00:00:02"})
            self.assertEqual(n.stations(9), set())
            with self.assertRaises(OSError):
                n.stations(5)                                              # no such interface
        self.assertEqual([f for _, _, f in k.sent], [nl.NLM_F_REQUEST] + [nl.NLM_F_REQUEST | nl.NLM_F_DUMP] * 4)

    def test_access_point(self):
        real = nl.Nl80211
        with mock.patch.object(nl, "Nl80211", lambda: real(sock=self.kernel())):
            self.assertEqual(nl.access_point(), ("wlP1p1s0", {"a4:f9:21:00:00:01", "02:00:00:00:00:02"}))
        k = Kernel({9: ("wlan1", 2)}, {9: []})                             # joined to a network
        with mock.patch.object(nl, "Nl80211", lambda: real(sock=k)):
            self.assertIsNone(nl.access_point())

    def test_this_kernel_answers(self):
        try:
            n = nl.Nl80211()
        except OSError as e:                                               # no Wi-Fi driver on this machine
            self.skipTest(str(e))
        with n:
            for i in n.interfaces():
                self.assertIsInstance(n.stations(i["ifindex"]), set)


ROOT = Path(__file__).resolve().parent.parent


class ClosedNetworkTest(unittest.TestCase):
    """deploy/aio-hotspot-dispatcher: nothing is forwarded to or from the hotspot while it runs
    (NetworkManager's "shared" mode turns forwarding on), with nft or else iptables."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log, self.rules = self.dir / "log", self.dir / "rules"
        bin_ = self.dir / "bin"
        bin_.mkdir()
        (bin_ / "nft").write_text('#!/bin/sh\necho "nft $*" >> "$LOG"\n[ "$1" = -f ] && cat >> "$LOG"\nexit ${NFT_RC:-0}\n')
        (bin_ / "iptables").write_text('#!/bin/sh\necho "iptables $*" >> "$LOG"\n'
                                       '[ "$1" = -S ] && cat "$RULES" 2>/dev/null\nexit 0\n')
        for f in bin_.iterdir():
            f.chmod(0o755)
        self.path = f"{bin_}:/usr/bin:/bin"

    def run_script(self, action, connection="aio-hotspot", nft_rc=0):
        env = dict(os.environ, PATH=self.path, LOG=str(self.log), RULES=str(self.rules),
                   CONNECTION_ID=connection, NFT_RC=str(nft_rc))
        r = subprocess.run(["sh", str(ROOT / "deploy" / "aio-hotspot-dispatcher"), "wlan0", action], env=env,
                           capture_output=True, text=True, timeout=10)
        log = self.log.read_text() if self.log.exists() else ""
        self.log.unlink(missing_ok=True)
        return r.returncode, log

    def test_other_connections_and_actions_are_left_alone(self):
        self.assertEqual(self.run_script("up", connection="office"), (0, ""))
        self.assertEqual(self.run_script("pre-up"), (0, ""))

    def test_up_drops_what_would_be_forwarded(self):
        rc, log = self.run_script("up")
        self.assertEqual(rc, 0)
        self.assertIn("nft delete table inet aio_hotspot", log)
        self.assertIn('iifname "wlan0" drop', log)
        self.assertIn('oifname "wlan0" drop', log)
        self.assertNotIn("iptables -I", log)

    def test_iptables_where_nft_does_not_work(self):
        rc, log = self.run_script("up", nft_rc=1)
        self.assertEqual(rc, 0)
        for d in ("-i", "-o"):
            self.assertIn(f"iptables -I FORWARD 1 {d} wlan0 -m comment --comment aio-hotspot -j DROP", log)

    def test_down_removes_only_its_own_rules(self):
        self.rules.write_text("-P FORWARD DROP\n-A FORWARD -o wlan0 -m comment --comment aio-hotspot -j DROP\n"
                              "-A FORWARD -j DOCKER-USER\n")
        rc, log = self.run_script("down")
        self.assertEqual(rc, 0)
        self.assertIn("nft delete table inet aio_hotspot", log)
        self.assertIn("iptables -D FORWARD -o wlan0 -m comment --comment aio-hotspot -j DROP", log)
        self.assertNotIn("DOCKER-USER", log.replace("iptables -S FORWARD", ""))
        self.assertNotIn("nft -f", log)


class InstallerTest(unittest.TestCase):
    """install.sh says what a system lacks for the hotspot (a fresh system may have no dnsmasq)."""

    def dry_run(self, tools):
        with tempfile.TemporaryDirectory() as tmp:
            bin_ = Path(tmp) / "bin"
            bin_.mkdir()
            for t in tools:
                (bin_ / t).write_text("#!/bin/sh\nexit 0\n")
                (bin_ / t).chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_}:/usr/bin:/bin")
            r = subprocess.run(["bash", str(ROOT / "deploy" / "install.sh"), "--dry-run", "--prefix", f"{tmp}/opt",
                                "--with-hotspot", "--without-sysmon", "--without-replay"],
                               env=env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def test_hotspot_left_out_without_dnsmasq(self):
        if Path("/usr/bin/dnsmasq").exists() or Path("/bin/dnsmasq").exists():
            self.skipTest("dnsmasq is in /usr/bin here")
        rc, out = self.dry_run(["nmcli", "nft"])
        self.assertEqual(rc, 0, out)
        self.assertIn("hotspot (Wi-Fi hotspot (Maintenance > Network)) left out: this system has no dnsmasq", out)
        self.assertIn("optional parts: wifi", out)

    def test_hotspot_with_what_it_needs(self):
        rc, out = self.dry_run(["nmcli", "nft", "dnsmasq"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("left out", out)
        self.assertIn("optional parts: wifi hotspot", out)


if __name__ == "__main__":
    unittest.main()
