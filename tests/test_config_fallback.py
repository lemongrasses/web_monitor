"""A settings file with an error falls back to the last good settings, then the defaults."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard import config as C


class FallbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        p = mock.patch.object(C, "LAST_GOOD", str(self.dir / "state" / "dashboard.lastgood.yaml"))
        p.start()
        self.addCleanup(p.stop)
        self.cfg_file = self.dir / "dashboard.yaml"

    def write(self, text):
        self.cfg_file.write_text(text, encoding="utf-8")
        return str(self.cfg_file)

    def test_good_file_is_used_and_remembered(self):
        cfg, problem = C.load_config_safely(self.write("product: {port: 18080}\n"))
        self.assertEqual((problem, cfg["product"]["port"]), ("", 18080))
        C.remember_good(cfg)
        self.assertTrue((self.dir / "state" / "dashboard.lastgood.yaml").exists())

    def test_broken_file_falls_back_to_the_last_good_one(self):
        cfg, _ = C.load_config_safely(self.write("product: {port: 18080}\n"))
        C.remember_good(cfg)
        cfg, problem = C.load_config_safely(self.write("product: {port: 18080\n  bad: [\n"))   # YAML error
        self.assertIn("running with the last good settings", problem)
        self.assertEqual(cfg["product"]["port"], 18080)

    def test_without_a_good_copy_the_defaults_run(self):
        cfg, problem = C.load_config_safely(self.write("- just\n- a list\n"))
        self.assertIn("must be a YAML mapping", problem)
        self.assertIn("built-in defaults", problem)
        self.assertEqual(cfg["product"]["port"], C.DEFAULTS["product"]["port"])
        self.assertTrue(cfg["autostart"]["enabled"])

    def test_missing_file_falls_back_too(self):
        cfg, problem = C.load_config_safely(str(self.dir / "nope.yaml"))
        self.assertIn("unusable", problem)


class ValueKindTest(unittest.TestCase):
    def test_wrong_kinds_are_replaced_by_defaults_and_reported(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "dashboard.yaml"
            f.write_text("autostart:\n  stale_grace_s: soon\n  enabled: yes-please\n"
                         "  prerequisites: imu\n  max_attempts: 0\n"
                         "dso_watchdog: {max_memory_mb: 2500}\ndata: {roots: [/data]}\n"
                         "product: {port: 18080}\n", encoding="utf-8")
            cfg = C.load_config(str(f))
        a = cfg["autostart"]
        self.assertEqual((a["stale_grace_s"], a["enabled"], a["prerequisites"], a["max_attempts"]),
                         (10.0, True, ["imu"], 0))
        self.assertEqual(len(cfg.problems), 3)
        self.assertTrue(any("autostart.stale_grace_s: 'soon'" in p for p in cfg.problems))
        # free values ("auto" defaults) and good values are kept
        self.assertEqual((cfg["dso_watchdog"]["max_memory_mb"], cfg["data"]["roots"], cfg["product"]["port"]),
                         (2500, [{"path": "/data"}], 18080))

    def test_data_roots_as_plain_paths(self):
        """roots: [/data] crashed the dashboard at start (it expected {path: ...} entries)."""
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "dashboard.yaml"
            f.write_text("data: {roots: [/data, {path: /logs, name: Logs}, 7]}\n", encoding="utf-8")
            cfg = C.load_config(str(f))
        self.assertEqual(cfg["data"]["roots"], [{"path": "/data"}, {"path": "/logs", "name": "Logs"}])
        self.assertEqual(cfg.problems, ["data.roots: 7 is not a folder; skipped"])

    def test_the_shipped_settings_are_clean(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(C.load_config(str(root / "config" / "dashboard.yaml")).problems, [])


if __name__ == "__main__":
    unittest.main()
