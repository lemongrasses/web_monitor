import copy
import unittest

from aio_system_dashboard.collectors.gnss_diag import (ERROR, NOT_USED_AFTER_S, OK, STALE, WARN,
                                                       GnssDiagnostics, diagnose, error_hint,
                                                       parse_status)
from aio_system_dashboard.config import DEFAULTS, Config


class Store:
    def __init__(self):
        self.d = {}

    def set(self, k, v):
        self.d[k] = v


class Events:
    def __init__(self):
        self.added = []

    def add(self, level, category, title, detail="", **extra):
        self.added.append((level, category, title))


class Clock:
    t = 10.0

    def __call__(self):
        return self.t


def statuses(fix="SPP", diffage="0.0", sig=OK, cnr="41.0", pct="70", cor=OK, cor_msg="receiving corrections",
             error=""):
    """One DiagnosticArray as the OpenRTK330 driver sends it."""
    return [
        ("openrtk330: GNSS solution", OK if fix == "RTK fix" else WARN, fix,
         {"fix": fix, "position_type": "1", "satellites_seen": "32", "satellites_used": "30",
          "hdop": "0.60", "diffage_s": diffage, "sigma_lat_m": "10.000"}),
        ("openrtk330: GNSS signal", sig, "good signal",
         {"GPS.satellites": "9", "GPS.cnr_median_dbhz": "37.0", "GPS.pct_ge35": "57",
          "BeiDou.satellites": "7", "BeiDou.cnr_median_dbhz": "44.0", "BeiDou.pct_ge35": "100",
          "satellites": "32", "cnr_median_dbhz": cnr, "pct_ge35": pct}),
        ("openrtk330: RTK corrections", cor, cor_msg,
         {"state": "connected" if cor == OK else "waiting", "caster": "210.241.63.193:81",
          "mountpoint": "GNSS_Taiwan", "caster_rate_Bps": "980", "to_receiver_rate_Bps": "980",
          "last_error": error}),
    ]


def diag():
    data = copy.deepcopy(DEFAULTS)
    data["nav"]["aio_nav_config"] = ""
    clock, store, events = Clock(), Store(), Events()
    return GnssDiagnostics(Config(data, None), store, events, clock, wall=clock), clock, store, events


def titles(findings):
    return [f["title"] for f in findings]


class ParseTest(unittest.TestCase):
    def test_numbers_and_systems(self):
        s = parse_status(OK, "good signal", dict(statuses()[1][3]))
        self.assertEqual(s["pct_ge35"], 70.0)
        self.assertEqual([x["name"] for x in s["systems"]], ["GPS", "BeiDou"])     # constellation order
        self.assertEqual(s["systems"][1], {"name": "BeiDou", "satellites": 7.0, "cnr_median_dbhz": 44.0,
                                           "pct_ge35": 100.0})

    def test_text_fields_stay_text(self):
        s = parse_status(ERROR, "x", {"state": "waiting", "last_error": "TCP connect failed: timed out"})
        self.assertEqual(s["last_error"], "TCP connect failed: timed out")


class DiagnoseTest(unittest.TestCase):
    def snap(self, **kw):
        d, *_ = diag()
        d.on_array(statuses(**kw))
        return d.snapshot()

    def test_all_good_has_no_findings(self):
        snap = self.snap(fix="RTK fix")
        self.assertEqual(snap["findings"], [])
        self.assertEqual(snap["level"], "healthy")

    def test_weak_signal_points_at_the_antenna(self):
        """Measured 2026-10-31: median 20-28 dBHz, almost no signal >= 35: SPP whatever the corrections."""
        snap = self.snap(sig=ERROR, cnr="23.4", pct="8")
        self.assertEqual(titles(snap["findings"]), ["Very weak satellite signal"])
        self.assertIn("23 dBHz", snap["findings"][0]["detail"])
        self.assertIn("antenna", snap["findings"][0]["hint"])
        self.assertEqual(snap["level"], "fault")

    def test_no_internet_is_named(self):
        """Seen 15:52-16:08: the router answered but its uplink was down."""
        snap = self.snap(cor=ERROR, cor_msg="not connected: TCP connect failed: No route to host",
                         error="TCP connect failed: No route to host")
        self.assertEqual(titles(snap["findings"]), ["No RTK corrections"])
        self.assertIn("router's uplink", snap["findings"][0]["hint"])

    def test_error_hints(self):
        self.assertIn("refused the login", error_hint("caster rejected request: HTTP/1.1 401 Unauthorized"))
        self.assertIn("mountpoint", error_hint("mountpoint 'X' not found (caster sent the sourcetable)"))
        self.assertIn("internet", error_hint("TCP connect failed: Network is unreachable"))
        self.assertEqual(error_hint("something else"), "")

    def test_rtk_off_is_info_only(self):
        snap = self.snap(cor=STALE, cor_msg="RTK disabled (RTK_ENABLE false)")
        self.assertEqual([f["level"] for f in snap["findings"]], ["info"])

    def test_no_message_from_the_driver(self):
        d, clock, store, _ = diag()
        self.assertEqual(titles(d.snapshot()["findings"]), ["No GNSS diagnostics from the receiver driver"])
        d.on_array(statuses())
        clock.t += 6                                       # past timeout_s (5 s)
        snap = d.snapshot()
        self.assertFalse(snap["available"])
        self.assertIn("for 6 s", snap["reason"])

    def test_unrelated_statuses_are_ignored(self):
        d, *_ = diag()
        d.on_array([("other: thing", OK, "", {})])
        self.assertFalse(d.snapshot()["available"])


class NotUsedTest(unittest.TestCase):
    def test_good_inputs_but_spp_for_minutes(self):
        d, clock, *_ = diag()
        for _ in range(int(NOT_USED_AFTER_S) + 2):
            d.on_array(statuses(fix="SPP", diffage="0.0"))
            d.tick()
            clock.t += 1
        self.assertIn("Corrections arrive but the receiver does not use them", titles(d.snapshot()["findings"]))

    def test_the_count_restarts_when_rtk_comes(self):
        d, clock, *_ = diag()
        for _ in range(100):
            d.on_array(statuses(fix="SPP"))
            d.snapshot()
            clock.t += 1
        d.on_array(statuses(fix="RTK float", diffage="1.0"))
        self.assertEqual(d.snapshot()["not_used_s"], 0.0)
        d.on_array(statuses(fix="SPP"))
        self.assertEqual(d.snapshot()["not_used_s"], 0.0)


class EventsAndHistoryTest(unittest.TestCase):
    def test_correction_loss_and_recovery_are_logged_once(self):
        d, clock, store, events = diag()
        for cor in (OK, OK, ERROR, ERROR, OK):
            d.on_array(statuses(cor=cor, cor_msg="receiving corrections" if cor == OK else "not connected: x"))
            d.tick()
            clock.t += 1
        self.assertEqual(events.added, [("warning", "gnss", "RTK corrections lost"),
                                        ("success", "gnss", "RTK corrections restored")])
        self.assertEqual(len(store.d["gnss_diag"]["history"]), 5)
        self.assertEqual(store.d["gnss_diag"]["history"][-1]["cnr_median_dbhz"], 41.0)

    def test_history_is_bounded(self):
        d, clock, store, _ = diag()
        d.g["history_s"] = 600
        for _ in range(650):
            d.on_array(statuses())
            d.tick()
            clock.t += 1
        self.assertEqual(len(store.d["gnss_diag"]["history"]), 600)


class PageTest(unittest.TestCase):
    def setUp(self):
        try:
            from aio_system_dashboard.web.maintenance import create_maintenance_app
        except ImportError as e:
            self.skipTest(str(e))
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(Config, "state_file", lambda _s, n: Path(tmp.name) / n)
        p.start()
        self.addCleanup(p.stop)
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        data["maintenance"]["lock"] = "tools"
        from aio_system_dashboard.state import still
        d, *_ = diag()
        d.on_array(statuses(sig=ERROR, cnr="23.0", pct="8"))
        store = {"gnss_diag": d.snapshot()}
        cfg = Config(data, None)
        events = SimpleNamespace(add=lambda *a, **k: None, recent=lambda n: [])
        limits = still.StillLimits(cfg, Path(tmp.name) / "still_limits.json", events)
        inputs = lambda: (None, None, None)
        ctx = SimpleNamespace(cfg=cfg, modules=[],
                              store=SimpleNamespace(get=lambda k, default=None: store.get(k, default)),
                              actions=SimpleNamespace(describe=lambda: []),
                              preview=SimpleNamespace(describe=lambda: {}), events=events,
                              health=SimpleNamespace(motion_inputs=inputs), still_limits=limits,
                              still_calibration=still.StillCalibration(inputs, limits))
        app = create_maintenance_app(ctx)
        self.addCleanup(lambda: getattr(ctx, "terminals", None) and ctx.terminals.shutdown())
        self.c = app.test_client()

    def test_page_renders_and_is_in_the_sidebar(self):
        r = self.c.get("/gnss")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        for text in ("Receiver solution", "Antenna signal", "RTK corrections (NTRIP)", "Last 10 minutes",
                     'href="/gnss"'):
            self.assertIn(text, html)

    def test_snapshot_carries_the_findings(self):
        j = self.c.get("/api/maint/snapshot").get_json()
        self.assertEqual(titles(j["gnss_diag"]["findings"]), ["Very weak satellite signal"])


if __name__ == "__main__":
    unittest.main()
