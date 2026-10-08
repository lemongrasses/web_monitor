"""Maintenance tools: password lock, AIO NAV config editor, terminal sessions."""

import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aio_system_dashboard.actions import aionav_config as ac
from aio_system_dashboard.actions.terminal import TerminalManager
from aio_system_dashboard.config import DEFAULTS, Config

LIVE = """# comment at the top
aio_nav_node:
  ros__parameters:
    ros_domain_id: 10
    ros_localhost_only: 1
    use_sim_time: false
    imu_topic: "/imu"
    save_parsed_txt: true   # keep this comment
    output_rate: 100.0
    output_udp: "192.168.116.154:9000"
    Alignment:
      pos:
        option: 1
        ini_value: [22.9, 120.2, 59.7]
      coarse_duration: 10.0
    GNSS:
      leverarm: [-0.49, 0.0, 0.0]
"""
BAG = LIVE.replace("ros_domain_id: 10", "ros_domain_id: 13").replace("use_sim_time: false", "use_sim_time: true")


class ConfigEditTest(unittest.TestCase):
    def test_form_change_keeps_comments_and_layout(self):
        new = ac.apply_form(LIVE, {"output_rate": 50, "save_parsed_txt": False, "Alignment.pos.option": 2,
                                   "GNSS.leverarm": [-0.5, 0.1, 0], "imu_topic": "/imu2"})
        self.assertIn("# comment at the top", new)
        self.assertIn("    save_parsed_txt: false   # keep this comment\n", new)
        self.assertIn("        option: 2\n", new)
        self.assertIn("      leverarm: [-0.5, 0.1, 0.0]\n", new)
        self.assertIn('    imu_topic: "/imu2"\n', new)
        v = ac.form_values(new)
        self.assertEqual((v["output_rate"], v["save_parsed_txt"], v["Alignment.pos.option"]), (50.0, False, 2))
        self.assertEqual(len(new.splitlines()), len(LIVE.splitlines()))     # nothing else moved

    def test_form_rejects_bad_values_with_a_reason(self):
        for change, words in (({"output_rate": 0}, "between"), ({"imu_topic": "imu"}, "starts with /"),
                              ({"output_udp": "x"}, "host:port"), ({"GNSS.leverarm": [1, 2]}, "valid"),
                              ({"nope": 1}, "unknown"), ({"Motion.zupt": True}, "not in this file")):
            with self.assertRaises(ac.ConfigError) as e:
                ac.apply_form(LIVE, change)
            self.assertIn(words, str(e.exception), change)

    def test_validate(self):
        self.assertEqual(ac.validate(LIVE), ([], []))
        self.assertIn("line", ac.validate("a: [1,")[0][0])
        self.assertTrue(ac.validate("x: 1")[0])                               # no ros__parameters
        errors, _ = ac.validate(LIVE.replace("output_rate: 100.0", 'output_rate: "fast"'))
        self.assertTrue(errors)


class ConfigFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name) / "install" / "aio_nav_ros" / "share" / "aio_nav_ros" / "config"
        d.mkdir(parents=True)
        (d / "aio_nav.yaml").write_text(LIVE)
        (d / "aio_nav_bag.yaml").write_text(BAG)
        self.dir = d
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = str(d / "aio_nav.yaml")
        data["ros"]["mode"] = "live"
        state = Path(self.tmp.name) / "state"
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: state / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: state / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = Config(data, None)

    def test_only_the_mode_files_are_offered(self):
        files = {f["mode"]: f for f in ac.files(self.cfg)}
        self.assertEqual((files["live"]["name"], files["bag"]["name"]), ("aio_nav.yaml", "aio_nav_bag.yaml"))
        self.assertTrue(files["live"]["active"])
        with self.assertRaises(ac.ConfigError):
            ac.file_for(self.cfg, "../../etc/passwd")

    def test_save_backup_conflict_and_restore(self):
        f = ac.file_for(self.cfg, "live")
        new = ac.apply_form(LIVE, {"output_rate": 50})
        r = ac.save(self.cfg, "live", new, f["mtime_ns"])
        self.assertTrue(r["changed"])
        self.assertIn("+    output_rate: 50.0", r["diff"])
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), new)
        self.assertEqual(self.cfg.expected_nav_rate, 50.0)                   # the dashboard follows at once
        backups = ac.backups(self.cfg, "aio_nav.yaml")
        self.assertEqual(ac.read_backup(self.cfg, "aio_nav.yaml", backups[0]["id"]), LIVE)
        with self.assertRaises(ac.ConfigError):                              # opened before the save
            ac.save(self.cfg, "live", LIVE, f["mtime_ns"])
        with self.assertRaises(ac.ConfigError):                              # broken text is not written
            ac.save(self.cfg, "live", "x: [", None)
        for bad in ("../aio_nav.yaml", "aio_nav.yaml.x.bak", "aio_nav_bag.yaml.20260101-000000.bak"):
            with self.assertRaises(ac.ConfigError):
                ac.read_backup(self.cfg, "aio_nav.yaml", bad)
        self.assertFalse(ac.save(self.cfg, "live", new, None)["changed"])   # same text: nothing to do


class GuardWebTest(unittest.TestCase):
    """Password lock and the protected routes, through the Maintenance app."""

    def setUp(self):
        try:
            from aio_system_dashboard.web.maintenance import create_maintenance_app
            from aio_system_dashboard.web.auth import write_password_file
        except ImportError as e:
            self.skipTest(str(e))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        d = base / "install" / "aio_nav_ros" / "share" / "aio_nav_ros" / "config"
        d.mkdir(parents=True)
        (d / "aio_nav.yaml").write_text(LIVE)
        (d / "aio_nav_bag.yaml").write_text(BAG)
        write_password_file(str(base / "pw"), "right-password")
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = str(d / "aio_nav.yaml")
        data["ros"]["mode"] = "live"
        data["maintenance"]["password_file"] = str(base / "pw")
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: base / "state" / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: base / "state" / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.events = []
        ctx = SimpleNamespace(cfg=Config(data, None), modules=[], store=SimpleNamespace(get=lambda k, d=None: d),
                              actions=SimpleNamespace(describe=lambda: []), preview=SimpleNamespace(describe=lambda: {}),
                              events=SimpleNamespace(add=lambda *a, **k: self.events.append(a[2])))
        self.ctx = ctx
        self.app = create_maintenance_app(ctx)
        self.addCleanup(lambda: ctx.terminals.shutdown())
        self.c = self.app.test_client()
        self.h = {"X-Requested-With": "aio-dashboard"}

    def post(self, url, body):
        return self.c.post(url, json=body, headers=self.h)

    def test_locked_until_the_right_password(self):
        save = {"form": {"output_rate": 50}}
        self.assertEqual(self.post("/api/maint/aionav/live/save", save).status_code, 401)
        self.assertEqual(self.post("/api/maint/term", {}).status_code, 401)
        self.assertEqual(self.post("/api/maint/auth/unlock", {"password": "wrong"}).status_code, 403)
        r = self.post("/api/maint/auth/unlock", {"password": "right-password"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])
        r = self.post("/api/maint/aionav/live/save", save)
        self.assertTrue(r.get_json()["success"], r.get_json())
        self.assertIn("Maintenance unlock failed", self.events)
        self.assertIn("AIO NAV config aio_nav.yaml saved", self.events)
        self.post("/api/maint/auth/lock", {})
        self.assertEqual(self.post("/api/maint/aionav/live/save", save).status_code, 401)

    def test_repeated_wrong_passwords_lock_out(self):
        for _ in range(5):
            self.post("/api/maint/auth/unlock", {"password": "wrong"})
        r = self.post("/api/maint/auth/unlock", {"password": "right-password"})
        self.assertEqual(r.status_code, 429)

    def test_no_password_file_means_locked(self):
        os.unlink(self.ctx.guard.password_file)
        self.assertFalse(self.c.get("/api/maint/auth").get_json()["configured"])
        self.assertEqual(self.post("/api/maint/auth/unlock", {"password": ""}).status_code, 403)

    def test_preview_needs_no_password_and_writes_nothing(self):
        r = self.post("/api/maint/aionav/live/preview", {"form": {"output_rate": 50}}).get_json()
        self.assertTrue(r["success"] and r["changed"])
        self.assertIn("output_rate: 50.0", r["diff"])
        self.assertIn("output_rate: 100.0", self.c.get("/api/maint/aionav/live").get_json()["text"])

    def test_terminal_belongs_to_the_browser_that_opened_it(self):
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        sid = self.post("/api/maint/term", {"cols": 80, "rows": 24}).get_json()["id"]
        other = self.app.test_client()
        r = other.post(f"/api/maint/term/{sid}/input", json={"data": "x"}, headers=self.h,
                       environ_base={"REMOTE_ADDR": "10.0.0.9"})
        self.assertEqual(r.status_code, 401)                 # no session there at all
        self.assertTrue(self.post(f"/api/maint/term/{sid}/input", {"data": "exit\r"}).get_json()["success"])


class TerminalManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = TerminalManager(Path(self.tmp.name) / "terms.json", max_sessions=2)
        self.addCleanup(self.m.shutdown)

    def output_until(self, s, text, timeout=8.0):
        out, pos, end = b"", 0, time.time() + timeout
        while time.time() < end and text.encode() not in out:
            data, pos = s.read(pos, 0.3)
            out += data
        return out.decode(errors="replace")

    def test_shell_runs_commands_with_a_clean_environment(self):
        sid = self.m.open("127.0.0.1", 90, 20)["id"]
        s = self.m.get(sid)
        self.m.write(sid, "echo R=$AIO_DASHBOARD_TERMINAL:${PYTHONPATH:-none}:$(tput cols)\r")
        self.assertIn("R=1:none:90", self.output_until(s, "R=1"))
        self.m.resize(sid, 120, 30)
        self.m.write(sid, "exit\r")
        for _ in range(50):
            if not s.alive:
                break
            time.sleep(0.1)
        self.assertFalse(s.alive)

    def test_limit_close_and_orphans(self):
        a = self.m.open("127.0.0.1")["id"]
        self.m.open("127.0.0.1")
        self.assertFalse(self.m.open("127.0.0.1")["success"])       # at most 2 here
        pid = self.m.get(a).pid
        self.assertEqual(json.loads(self.m.state_file.read_text()).count(pid), 1)
        time.sleep(0.5)                                              # let both shells start
        # a new manager (dashboard restarted) closes what the old one left
        other = TerminalManager(self.m.state_file)
        self.addCleanup(other.shutdown)
        self.assertEqual(other.cleanup_orphans(), 2)
        for _ in range(50):
            if not self.m.get(a).alive:
                break
            time.sleep(0.1)
        self.assertFalse(self.m.get(a).alive)


if __name__ == "__main__":
    unittest.main()
