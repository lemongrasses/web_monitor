"""The environment (state/environment.py): network, RTK and vehicle, detected or set on a page."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.state import environment as env

OK, WARN, ERROR, STALE = 0, 1, 2, 3
NET_WIRED = {"internet": {"reachable": True}, "default_route": {"interface": "eno1"}}
NET_NONE = {"internet": {"reachable": False, "error": "no default route"}}
DIAG_NTRIP_OFF = {"available": True, "corrections": {"level": STALE}}
DIAG_NTRIP_ERROR = {"available": True, "corrections": {"level": ERROR}}
DIAG_NTRIP_OK = {"available": True, "corrections": {"level": OK}}
SPP = {"available": True, "quality": "spp"}
FIX = {"available": True, "quality": "fixed"}


class EnvTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        p = mock.patch.object(Config, "state_file", lambda _s, n: self.state / n)
        p.start()
        self.addCleanup(p.stop)
        y = self.state / "aio_nav.yaml"
        y.write_text("aio_nav_node:\n  ros__parameters:\n    output_udp: '192.0.2.10:9000'\n")
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["aio_nav_config"] = str(y)
        self.boot = "boot-1"
        self.t = 0.0

    def make(self):
        self.cfg = Config(self.data, None)
        return env.Environment(self.cfg, clock=lambda: self.t, boot_id=lambda: self.boot)

    def run_env(self, e, network, gnss, diag, seconds=0.0, step=1.0):
        out = e.evaluate(network, gnss, diag, self.t)
        end = self.t + seconds
        while self.t < end:
            self.t += step
            out = e.evaluate(network, gnss, diag, self.t)
        return out


class DetectTest(EnvTestBase):
    def test_network(self):
        self.assertEqual(env.detect_network(NET_WIRED)[0], "wired")
        with mock.patch.object(env, "wireless", lambda i: i == "wlan0"):
            self.assertEqual(env.detect_network({"internet": {"reachable": True},
                                                 "default_route": {"interface": "wlan0"}})[0], "wifi")
        self.assertEqual(env.detect_network(NET_NONE)[0], "none")
        self.assertEqual(env.detect_network({"internet": {"reachable": None, "error": "not set up"}})[0], None)
        self.assertEqual(env.detect_network({})[0], None)

    def test_rtk_rules(self):
        self.assertEqual(env.detect_rtk(SPP, DIAG_NTRIP_OFF, "wired", False)[0], False)  # no NTRIP set up
        self.assertEqual(env.detect_rtk(SPP, DIAG_NTRIP_ERROR, "wired", False)[0], True)  # set up: expected
        self.assertEqual(env.detect_rtk(SPP, DIAG_NTRIP_ERROR, "none", False)[0], False)  # but no network
        self.assertEqual(env.detect_rtk(SPP, {}, "none", False)[0], False)
        self.assertEqual(env.detect_rtk(SPP, {}, "wired", False)[0], None)                # old driver: unknown
        self.assertEqual(env.detect_rtk(SPP, DIAG_NTRIP_OFF, "none", True)[0], True)      # seen this power-on

    def test_vehicle(self):
        self.cfg = Config(self.data, None)
        self.assertTrue(env.detect_vehicle(self.cfg)[0])
        (self.state / "aio_nav.yaml").write_text("aio_nav_node:\n  ros__parameters:\n    output_udp: ''\n")
        self.assertFalse(env.detect_vehicle(Config(self.data, None))[0])


class EnvironmentTest(EnvTestBase):
    def test_no_rtk_place_does_not_require_rtk(self):
        out = self.run_env(self.make(), NET_NONE, SPP, DIAG_NTRIP_ERROR)
        self.assertEqual((out["network"]["value"], out["rtk"]["value"]), ("none", False))
        self.assertEqual((out["rtk_required"], out["rtk_source"]), (False, "environment"))

    def test_unknown_requires_rtk(self):
        out = self.run_env(self.make(), {}, SPP, {})
        self.assertIsNone(out["rtk"]["value"])
        self.assertEqual((out["rtk_required"], out["rtk_source"]), (True, "environment not known yet"))

    def test_rtk_seen_holds_until_the_next_power_on(self):
        e = self.make()
        self.run_env(e, NET_WIRED, FIX, DIAG_NTRIP_OK)
        out = self.run_env(e, NET_NONE, SPP, DIAG_NTRIP_ERROR, seconds=60)    # under a bridge, no network
        self.assertTrue(out["rtk"]["value"])
        self.assertTrue(out["rtk_required"])                    # so losing it is DEGRADED, not "no RTK here"
        out = self.run_env(self.make(), NET_NONE, SPP, DIAG_NTRIP_ERROR)      # dashboard restart: kept
        self.assertTrue(out["rtk"]["value"])
        self.boot = "boot-2"                                    # power-on: decided afresh
        out = self.run_env(self.make(), NET_NONE, SPP, DIAG_NTRIP_ERROR)
        self.assertFalse(out["rtk"]["value"])

    def test_a_change_holds_ten_seconds(self):
        e = self.make()
        self.assertTrue(self.run_env(e, NET_WIRED, SPP, DIAG_NTRIP_ERROR)["rtk"]["value"])
        out = self.run_env(e, NET_NONE, SPP, DIAG_NTRIP_ERROR, seconds=5)
        self.assertTrue(out["rtk"]["value"])                    # network gone for 5 s: still RTK
        out = self.run_env(e, NET_NONE, SPP, DIAG_NTRIP_ERROR, seconds=6)
        self.assertFalse(out["rtk"]["value"])

    def test_a_page_override_wins_and_can_go_back_to_automatic(self):
        e = self.make()
        env.set_override(self.cfg, "rtk", False, "user page 192.0.2.1")
        out = self.run_env(e, NET_WIRED, SPP, DIAG_NTRIP_OK)
        self.assertEqual((out["rtk"]["value"], out["rtk"]["auto"], out["rtk"]["detected"]), (False, False, True))
        self.assertEqual((out["rtk_required"], out["rtk_source"]), (False, "set on a page"))
        env.set_override(self.cfg, "rtk", None, "user page 192.0.2.1")
        out = self.run_env(e, NET_WIRED, SPP, DIAG_NTRIP_OK)
        self.assertEqual((out["rtk"]["value"], out["rtk"]["auto"], out["rtk_required"]), (True, True, True))
        env.set_override(self.cfg, "vehicle", False, "maintenance")
        self.assertEqual(self.run_env(e, NET_WIRED, SPP, DIAG_NTRIP_OK)["vehicle"]["value"], False)
        with self.assertRaises(ValueError):
            env.set_override(self.cfg, "network", True, "x")

    def test_the_config_can_pin_rtk(self):
        self.data["readiness"]["require_rtk"] = False
        out = self.run_env(self.make(), NET_WIRED, SPP, DIAG_NTRIP_OK)
        self.assertEqual((out["rtk_required"], out["rtk_source"]), (False, "config"))

    def test_the_old_rtk_switch_still_counts(self):
        (self.state / "readiness.json").write_text(json.dumps({"require_rtk": False, "changed_by": "x"}))
        out = self.run_env(self.make(), NET_WIRED, FIX, DIAG_NTRIP_OK)
        self.assertEqual((out["rtk_required"], out["rtk_source"]), (False, "set on a page"))
        env.set_override(self.cfg, "rtk", None, "y")             # a new choice replaces the old file
        self.assertFalse((self.state / "readiness.json").exists())
        self.assertTrue(self.run_env(self.make(), NET_WIRED, FIX, DIAG_NTRIP_OK)["rtk_required"])


class TranslationTest(EnvTestBase):
    def test_what_the_settings_page_shows_is_translated(self):
        from aio_system_dashboard.web import i18n
        texts = [env.detect_network(n)[1] for n in (NET_WIRED, NET_NONE, {}, {"internet": {"error": "not set up"}})]
        texts += [env.detect_rtk(SPP, d, n, seen)[1] for d in ({}, DIAG_NTRIP_OFF, DIAG_NTRIP_OK)
                  for n in ("wired", "none", None) for seen in (True, False)]
        self.cfg = Config(self.data, None)
        texts.append(env.detect_vehicle(self.cfg)[1])
        for t in set(texts):
            self.assertNotEqual(i18n.tr(t, "zh-TW"), t, t)


if __name__ == "__main__":
    unittest.main()
