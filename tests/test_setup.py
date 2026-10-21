"""Product UI > Setup: the lever arms of the Live config file (web/setup.py)."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aio_system_dashboard.actions import aionav_config as ac
from aio_system_dashboard.config import DEFAULTS, Config

LIVE = """# shipped config
aio_nav_node:
  ros__parameters:
    ros_domain_id: 10
    use_sim_time: false
    output_rate: 100.0
    Motion:
      zupt: true
      leverarm: [-2.25, 0.0, 1.52]   # IMU -> rear axle
    GNSS:
      leverarm: [-0.49, 0.0, 0.0]
"""
BAG = LIVE.replace("ros_domain_id: 10", "ros_domain_id: 13").replace("use_sim_time: false", "use_sim_time: true")
H = {"X-Requested-With": "aio-dashboard"}


class SetupTest(unittest.TestCase):
    def setUp(self):
        try:
            from aio_system_dashboard.web.product import create_product_app
        except ImportError as e:
            self.skipTest(str(e))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        d = base / "install" / "aio_nav_ros" / "share" / "aio_nav_ros" / "config"
        d.mkdir(parents=True)
        (d / "aio_nav.yaml").write_text(LIVE)
        (d / "aio_nav_bag.yaml").write_text(BAG)
        self.file = d / "aio_nav.yaml"
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = str(self.file)
        data["nav"]["allow_control"] = True
        data["ros"]["mode"] = "live"
        self.data = data
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: base / "state" / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: base / "state" / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.state = {"services": {"aio_nav": {"pids": []}}, "readiness": {"motion": {"still": True}}}
        self.events, self.ran, self.cleared = [], [], []
        ctx = SimpleNamespace(
            cfg=Config(data, None), store=SimpleNamespace(get=lambda k, d=None: self.state.get(k, d)),
            nav=SimpleNamespace(clear=self.cleared.append),
            events=SimpleNamespace(add=lambda *a, **k: self.events.append((a[2], a[3]))),
            actions=SimpleNamespace(run=lambda a, t: self.ran.append((a, t)) or {"success": True, "summary": "ok"}))
        self.ctx = ctx
        self.c = create_product_app(ctx).test_client()

    def get(self):
        return self.c.get("/api/setup/lever-arms").get_json()

    def post(self, body, headers=H):
        return self.c.post("/api/setup/lever-arms", json=body, headers=headers)

    def test_read_and_save(self):
        self.assertEqual(self.c.get("/setup").status_code, 200)
        j = self.get()
        self.assertEqual((j["vehicle"], j["gnss"], j["nav_running"]), ([-2.25, 0.0, 1.52], [-0.49, 0.0, 0.0], False))
        r = self.post({"vehicle": [-2.4, 0.1, 1.5], "gnss": [-0.5, 0, -0.35], "base_version": j["version"]}).get_json()
        self.assertTrue(r["saved"], r)
        text = self.file.read_text()
        self.assertIn("      leverarm: [-2.4, 0.1, 1.5]   # IMU -> rear axle\n", text)    # the comment stays
        self.assertIn("      leverarm: [-0.5, 0.0, -0.35]\n", text)
        self.assertEqual(ac.validate(text)[0], [])
        title, detail = self.events[-1]
        self.assertEqual(title, "Lever arms changed")
        self.assertIn("GNSS antenna [-0.49, 0, 0] -> [-0.5, 0, -0.35]", detail)
        self.assertEqual(len(ac.backups(self.ctx.cfg, "aio_nav.yaml")), 1)
        self.assertEqual(self.ran, [])                                         # Save alone: no restart
        self.assertEqual(self.post({"vehicle": [-2.4, 0.1, 1.5], "gnss": [-0.5, 0, -0.35],
                                    "base_version": j["version"]}).status_code, 409)   # opened before the save

    def test_bad_values_are_refused_and_nothing_written(self):
        v = self.get()["version"]
        for vehicle in ([1, 2], ["1", 2, 3], [True, 0, 0], [25, 0, 0], [float("nan"), 0, 0], None, "1,2,3"):
            r = self.post({"vehicle": vehicle, "gnss": [0, 0, 0], "base_version": v})
            self.assertEqual(r.status_code, 422, vehicle)
        self.assertEqual(self.post({"vehicle": [0, 0, 0], "gnss": [0, 0, 0]}).status_code, 409)   # no version
        self.assertEqual(self.post({"vehicle": [0, 0, 0], "gnss": [0, 0, 0], "base_version": v}, headers={}).status_code, 400)
        self.assertEqual(self.file.read_text(), LIVE)

    def test_restart_only_with_the_vehicle_still(self):
        self.state["services"]["aio_nav"]["pids"] = [42]
        self.state["readiness"]["motion"]["still"] = False
        body = {"vehicle": [-2.3, 0, 1.5], "gnss": [-0.49, 0, 0], "restart": True, "base_version": self.get()["version"]}
        r = self.post(body)
        self.assertEqual(r.status_code, 409)
        self.assertIn("stand still", r.get_json()["summary"])
        self.assertEqual(self.file.read_text(), LIVE)                          # nothing written
        self.state["readiness"]["motion"]["still"] = True
        r = self.post(body).get_json()
        self.assertTrue(r["success"], r)
        self.assertEqual(self.ran, [("restart_process", "nav_core")])
        self.assertEqual(len(self.cleared), 1)

    def test_turned_off(self):
        self.data["product"]["edit_lever_arms"] = False
        self.assertEqual(self.c.get("/api/setup/lever-arms").status_code, 403)
        self.assertEqual(self.post({"vehicle": [0, 0, 0], "gnss": [0, 0, 0], "base_version": "x"}).status_code, 403)
        self.assertEqual(self.file.read_text(), LIVE)


if __name__ == "__main__":
    unittest.main()
