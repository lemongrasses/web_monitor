"""Recording and storage on the user pages: is AIO NAV writing its logs, and how long the space lasts."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard.collectors.recording import RecordingCollector, scan
from aio_system_dashboard.config import DEFAULTS, Config
from aio_system_dashboard.state.health import HealthEngine
from aio_system_dashboard.state.store import StateStore
from tests.test_readiness import ALIGNED, Clock, Events, FakeNav


class RecordingCollectorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "output"
        self.out.mkdir()
        y = self.dir / "aio_nav.yaml"
        y.write_text(f"aio_nav_node:\n  ros__parameters:\n    fusion_txt_path: '{self.out}/fusion.txt'\n")
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["aio_nav_config"] = str(y)
        self.t, self.wall = 100.0, 1_800_000_000.0
        self.rec = RecordingCollector(StateStore(), Config(self.data, None),
                                      clock=lambda: self.t, wall=lambda: self.wall)

    def write(self, name, size, age_s=0.0):
        f = self.out / name
        f.write_bytes(b"x" * size)
        os.utime(f, (self.wall - age_s, self.wall - age_s))

    def test_writing_rate_and_duration(self):
        self.write("fusion.txt", 1000)
        r = self.rec.poll()
        self.assertTrue(r["writing"])
        self.assertEqual(r["folder"], str(self.out))
        for _ in range(10):
            self.t += 2
            self.wall += 2
            self.write("fusion.txt", (self.out / "fusion.txt").stat().st_size + 9000)
            r = self.rec.poll()
        self.assertAlmostEqual(r["rate_Bps"], 4500, delta=1)
        self.assertEqual((r["recording_s"], r["file"]), (20.0, "fusion.txt"))

    def test_not_writing_after_the_files_stop_changing(self):
        self.write("fusion.txt", 1000, age_s=60)
        r = self.rec.poll()
        self.assertFalse(r["writing"])
        self.assertEqual((r["last_write_age_s"], r["recording_s"]), (60.0, None))

    def test_one_level_of_sub_folders_and_no_folder(self):
        (self.out / "run1").mkdir()
        (self.out / "run1" / "imu.txt").write_bytes(b"x" * 10)
        self.assertEqual(scan(str(self.out))[0], 10)
        self.out.joinpath("run1", "imu.txt").unlink()
        os.rmdir(self.out / "run1")
        os.rmdir(self.out)
        self.assertFalse(self.rec.poll()["available"])

    def test_logs_switched_off_in_aio_nav(self):
        y = self.dir / "aio_nav.yaml"
        y.write_text("aio_nav_node:\n  ros__parameters:\n    save_fusion_txt: false\n    save_parsed_txt: false\n")
        rec = RecordingCollector(StateStore(), Config(self.data, None))
        self.assertEqual((rec.poll()["enabled"], rec.poll()["writing"]), (False, False))


class StepsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        y = self.tmp / "aio_nav.yaml"
        y.write_text("aio_nav_node:\n  ros__parameters:\n    output_rate: 100.0\n")
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: self.tmp / "state" / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: self.tmp / "state" / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.data = copy.deepcopy(DEFAULTS)
        self.data["nav"]["aio_nav_config"] = str(y)
        self.data["services"] = {"aio_nav": {"label": "AIO NAV", "unit": "aio-nav.service",
                                             "process_pattern": "x/aio_nav_node"}}
        self.store, self.nav, self.clock = StateStore(), FakeNav(), Clock()
        self.store.set("services", {"aio_nav": {"label": "AIO NAV", "state": "running", "pids": [4242],
                                                "unit_state": {"active": "inactive", "file_state": "disabled"}}})
        self.store.set("gnss", {"available": True, "quality": "spp", "sigma_h_m": 3.0})
        self.store.set("gnss_diag", {"available": True, "corrections": {"level": 3}})   # no NTRIP: no RTK here
        self.nav.latest, self.nav.age = dict(ALIGNED), 0.01
        self.eng = HealthEngine(Config(self.data, None), self.store, self.nav, Events(), clock=self.clock)

    def steps(self, seconds=6):
        for _ in range(int(seconds / 0.2)):
            self.clock.t += 0.2
            self.eng.step()
        return {c["key"]: c for c in self.store.get("product")["checks"]}

    def test_no_rtk_place_shows_gnss_grey_not_a_warning(self):
        s = self.steps()
        self.assertEqual((s["gnss"]["name"], s["gnss"]["level"], s["gnss"]["label"]), ("GNSS", "idle", "No RTK"))
        self.assertEqual(self.store.get("readiness")["state"], "READY")

    def test_recording_while_ready(self):
        self.store.set("recording", {"available": True, "enabled": True, "writing": True, "rate_Bps": 75000,
                                     "recording_s": 2530, "file": "fusion.txt", "folder": "/out"})
        rec = self.steps()["recording"]
        self.assertEqual((rec["level"], rec["label"]), ("healthy", "Writing"))
        self.assertIn({"k": "This recording", "v": "0:42:10"}, rec["rows"])
        self.assertIn({"k": "Write rate", "v": "4.5 MB/min"}, rec["rows"])
        self.store.set("recording", {"available": True, "enabled": True, "writing": False, "last_write_age_s": 30})
        self.assertEqual(self.steps(1)["recording"]["label"], "Not writing")   # in use and nothing kept: red
        self.assertEqual(self.steps(1)["recording"]["level"], "fault")

    def test_storage_levels_and_time_left(self):
        self.store.set("recording", {"available": True, "enabled": True, "writing": True, "rate_Bps": 75000})
        for free, percent, level, label in ((182e9, 40.0, "healthy", "182 GB free"),
                                            (14e9, 92.0, "warning", "Almost full"),
                                            (0.5e9, 99.5, "fault", "Full")):
            self.store.set("system", {"disks": [{"path": "/", "total": 500e9, "free": free, "percent": percent}]})
            st = self.steps(1)["storage"]
            self.assertEqual((st["level"], st["label"]), (level, label))
        self.store.set("system", {"disks": [{"path": "/", "total": 500e9, "free": 182e9, "percent": 40.0}]})
        self.assertIn({"k": "Recording time left", "v": "about 674 h"}, self.steps(1)["storage"]["rows"])

    def test_every_step_links_to_a_maintenance_page(self):
        for key, step in self.steps().items():
            self.assertTrue(step.get("maint", "").startswith("/"), key)


if __name__ == "__main__":
    unittest.main()
