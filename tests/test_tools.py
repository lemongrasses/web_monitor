"""Maintenance tools: password lock, AIO NAV config editor, terminal sessions."""

import copy
import json
import os
import subprocess
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

# Stands in for aio_nav_node in a test start (args: --ros-args --params-file FILE -p config_file:=FILE).
# It refuses to run unless it is cut off from every network and the copy sends nothing to the vehicle.
FAKE_NODE = r"""#!/bin/bash
f="$3"
[ "$(tail -n +3 /proc/net/dev | wc -l)" = 1 ] || { echo "[FATAL] [1.0] [aio_nav_node]: not isolated"; exit 1; }
grep -q 'output_udp: ""' "$f" || { echo "[FATAL] [1.0] [aio_nav_node]: output_udp kept"; exit 1; }
grep -q BROKEN "$f" && { echo "[FATAL] [1.0] [aio_nav_node]: parameter 'x' has invalid type"; exit 1; }
echo "[WARN] [1.0] [aio_nav_node]: something to note"
echo "[INFO] [1.0] [aio_nav_node]: aio_nav_node ready"
exec sleep 30
"""


def install_fake_node(config_dir: Path) -> bool:
    """Put FAKE_NODE where test_start looks for aio_nav_node; False if this system cannot isolate it."""
    if subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode != 0:
        return False
    lib = config_dir.parents[2] / "lib" / "aio_nav_ros"
    lib.mkdir(parents=True)
    (lib / "aio_nav_node").write_text(FAKE_NODE)
    (lib / "aio_nav_node").chmod(0o755)
    (lib / "aio_nav_env.sh").write_text("aio_nav_source_ros() { :; }\n")
    ac._TESTED.clear()
    return True


def processes_under(folder: str) -> list:
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if folder.encode() in Path(f"/proc/{pid}/cmdline").read_bytes():
                out.append(pid)
        except OSError:
            pass
    return out


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

    def test_compare_with_the_file_it_replaces(self):
        """What AIO NAV was seen to refuse (2026-10-21: 33 broken copies started on the device)."""
        for old, new, words in (("output_rate: 100.0", "output_rate: 100", "100.0, not 100"),
                                ("      coarse_duration: 10.0\n", "", "was removed"),
                                ("coarse_duration: 10.0", "coarse_duration:", "no value"),
                                ("output_rate: 100.0", "output_rate: abc", "must be a number"),
                                ("use_sim_time: false", "use_sim_time: 0", "true or false"),
                                ('imu_topic: "/imu"', "imu_topic: 5", "must be text"),
                                ("leverarm: [-0.49, 0.0, 0.0]", "leverarm: [-0.49, 0.0]", "must have 3 values"),
                                ("leverarm: [-0.49, 0.0, 0.0]", "leverarm: 1.0", "list of 3"),
                                ("    Alignment:\n      pos:", "    Alignment: 5\n    Alignment_x:\n      pos:", "section"),
                                ("ini_value: [22.9, 120.2, 59.7]", "ini_value: [22.9, x, 59.7]", "must be a number"),
                                ("aio_nav_node:", "aio_nav:", "first line")):
            errors, _ = ac.compare(LIVE, LIVE.replace(old, new, 1))
            self.assertTrue(any(words in e for e in errors), (new, errors))
        for old, new in (("output_rate: 100.0", "output_rate: 1.0e2"), ("output_rate: 100.0", "output_rate: 1e2"),
                         ("option: 1", "option: 2"), ('imu_topic: "/imu"', "imu_topic: /imu2"),
                         ("use_sim_time: false", "use_sim_time: true"),
                         ("ini_value: [22.9, 120.2, 59.7]", "ini_value: [23.0, 120.0, 60.0]")):
            self.assertEqual(ac.compare(LIVE, LIVE.replace(old, new, 1)), ([], []), new)
        _, warnings = ac.compare(LIVE, LIVE.replace("output_rate: 100.0", "output_rate: 100.0\n    outptu_rate: 5.0"))
        self.assertIn("outptu_rate is new", warnings[0])

    def test_a_list_mixing_whole_numbers_and_decimals_is_refused(self):
        errors, _ = ac.validate(LIVE.replace("ini_value: [22.9, 120.2, 59.7]", "ini_value: [22, 120.2, 59.7]"))
        self.assertIn("decimal point", errors[0])

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
        r = ac.save(self.cfg, "live", new, f["version"])
        self.assertTrue(r["changed"])
        self.assertIn("+    output_rate: 50.0", r["diff"])
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), new)
        self.assertEqual(r["version"], ac.file_for(self.cfg, "live")["version"])
        self.assertEqual(self.cfg.expected_nav_rate, 50.0)                   # the dashboard follows at once
        backups = ac.backups(self.cfg, "aio_nav.yaml")
        self.assertEqual(ac.read_backup(self.cfg, "aio_nav.yaml", backups[0]["id"]), LIVE)
        with self.assertRaises(ac.ConflictError):                            # opened before the save
            ac.save(self.cfg, "live", LIVE, f["version"])
        with self.assertRaises(ac.ConfigError):                              # broken text is not written
            ac.save(self.cfg, "live", "x: [", None)
        for bad in ("../aio_nav.yaml", "aio_nav.yaml.x.bak", "aio_nav_bag.yaml.20260101-000000.bak"):
            with self.assertRaises(ac.ConfigError):
                ac.read_backup(self.cfg, "aio_nav.yaml", bad)
        self.assertFalse(ac.save(self.cfg, "live", new, None)["changed"])   # same text: nothing to do

    def test_a_file_aio_nav_would_refuse_is_not_saved(self):
        f = ac.file_for(self.cfg, "live")
        with self.assertRaises(ac.ConfigError) as e:
            ac.save(self.cfg, "live", LIVE.replace("      coarse_duration: 10.0\n", ""), f["version"])
        self.assertIn("was removed", str(e.exception))
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), LIVE)
        self.assertEqual(ac.backups(self.cfg, "aio_nav.yaml"), [])

    def test_a_broken_file_on_disk_does_not_block_its_repair(self):
        broken = LIVE.replace("coarse_duration: 10.0", "coarse_duration: abc")
        (self.dir / "aio_nav.yaml").write_text(broken)
        refused = {"ran": True, "ok": False, "detail": "bad conversion", "said": []}
        started = {"ran": True, "ok": True, "detail": "it reported ready after 0.8 s", "said": []}
        with mock.patch.object(ac, "test_start", side_effect=lambda _p, text: refused if text == broken else started):
            r = ac.save(self.cfg, "live", LIVE, ac.file_for(self.cfg, "live")["version"])
        self.assertTrue(r["changed"])
        self.assertTrue(any("does not start AIO NAV either" in w for w in r["warnings"]), r["warnings"])
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), LIVE)

    def test_aio_nav_is_test_started_isolated_before_saving(self):
        if not install_fake_node(self.dir):
            self.skipTest("this system cannot start a program without network (unshare -rn)")
        f = ac.file_for(self.cfg, "live")
        with self.assertRaises(ac.ConfigError) as e:
            ac.save(self.cfg, "live", LIVE.replace("output_rate: 100.0", "output_rate: 100.0  # BROKEN"), f["version"])
        self.assertIn("does not start with this file: parameter 'x' has invalid type", str(e.exception))
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), LIVE)
        r = ac.save(self.cfg, "live", ac.apply_form(LIVE, {"output_rate": 50}), f["version"])
        self.assertEqual((r["test_start"]["ok"], r["changed"]), (True, True), r["test_start"])
        self.assertIn("AIO NAV said: something to note", r["warnings"])
        self.assertEqual(processes_under(str(self.tmp.name)), [])          # nothing left running
        self.assertFalse(ac.test_start("/nonexistent/a/b/c/aio_nav.yaml", LIVE)["ran"])

    def test_version_survives_a_browser_round_trip(self):
        """Regression: the version used to be st_mtime_ns (~1.8e18). A browser keeps numbers exact
        only up to 2**53, so it sent back a rounded value and every save was a false conflict."""
        path = self.dir / "aio_nav.yaml"
        os.utime(path, ns=(1791266745638123456, 1791266745638123456))
        f = ac.file_for(self.cfg, "live")
        self.assertIsInstance(f["version"], str)
        back = json.loads(json.dumps({"base_version": f["version"]}))["base_version"]   # what JS returns
        r = ac.save(self.cfg, "live", ac.apply_form(LIVE, {"output_rate": 50}), back)
        self.assertTrue(r["changed"])

    def test_two_editors_the_second_gets_a_conflict(self):
        opened = ac.file_for(self.cfg, "live")["version"]                    # both open the same file
        first = ac.apply_form(LIVE, {"output_rate": 50})
        ac.save(self.cfg, "live", first, opened)
        with self.assertRaises(ac.ConflictError):
            ac.save(self.cfg, "live", ac.apply_form(LIVE, {"output_rate": 20}), opened)
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), first)   # the first save is kept

    def test_invalid_text_writes_nothing_and_keeps_no_backup(self):
        with self.assertRaises(ac.ConfigError):
            ac.save(self.cfg, "live", "x: [", ac.file_for(self.cfg, "live")["version"])
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), LIVE)
        self.assertEqual(ac.backups(self.cfg, "aio_nav.yaml"), [])

    def test_failed_replace_keeps_the_original_and_no_temp_file(self):
        f = ac.file_for(self.cfg, "live")
        with mock.patch("aio_system_dashboard.actions.aionav_config.os.replace",
                        side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                ac.save(self.cfg, "live", ac.apply_form(LIVE, {"output_rate": 50}), f["version"])
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), LIVE)
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")], [])

    def test_read_only_folder_is_reported_before_writing(self):
        if os.geteuid() == 0:
            self.skipTest("root may write anywhere")
        os.chmod(self.dir, 0o555)
        self.addCleanup(os.chmod, self.dir, 0o755)
        f = ac.file_for(self.cfg, "live")
        self.assertFalse(f["writable"])
        with self.assertRaises(ac.ConfigError) as e:
            ac.save(self.cfg, "live", ac.apply_form(LIVE, {"output_rate": 50}), f["version"])
        self.assertIn("folder", str(e.exception))
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), LIVE)

    def test_fifty_saves_in_a_row(self):
        """Every save gets a new version, the file always parses, and same-second backups
        do not overwrite each other (newest first)."""
        version, texts = ac.file_for(self.cfg, "live")["version"], [LIVE]
        for i in range(50):
            text = ac.apply_form(texts[-1], {"output_rate": 10 + i})
            r = ac.save(self.cfg, "live", text, version)
            self.assertNotEqual(r["version"], version)
            version = r["version"]
            texts.append(text)
            self.assertEqual(ac.validate((self.dir / "aio_nav.yaml").read_text())[0], [])
        self.assertEqual((self.dir / "aio_nav.yaml").read_text(), texts[-1])
        kept = ac.backups(self.cfg, "aio_nav.yaml")
        self.assertEqual(len(kept), ac.MAX_BACKUPS)
        # the newest backup is the text just before the last save
        self.assertEqual(ac.read_backup(self.cfg, "aio_nav.yaml", kept[0]["id"]), texts[-2])
        self.assertTrue(all(ac.read_backup(self.cfg, "aio_nav.yaml", b["id"]) for b in kept))


class GuardWebTest(unittest.TestCase):
    """Password lock and the protected routes, through the Maintenance app."""

    LOCK = "tools"                      # these tests: only the config editor and terminal are locked

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
        data["maintenance"]["lock"] = self.LOCK
        for p in (mock.patch.object(Config, "state_file", lambda _s, n: base / "state" / n),
                  mock.patch.object(Config, "ros_mode_file", lambda _s: base / "state" / "ros_mode.json")):
            p.start()
            self.addCleanup(p.stop)
        self.events = []
        ctx = SimpleNamespace(cfg=Config(data, None), modules=[], store=SimpleNamespace(get=lambda k, d=None: d),
                              actions=SimpleNamespace(describe=lambda: []), preview=SimpleNamespace(describe=lambda: {}),
                              events=SimpleNamespace(add=lambda *a, **k: self.events.append(a[2]),
                                                     recent=lambda n: []))
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
        r = self.post("/api/maint/aionav/live/save", save)               # no version: an old page
        self.assertEqual(r.status_code, 409)
        save["base_version"] = self.c.get("/api/maint/aionav/live").get_json()["file"]["version"]
        r = self.post("/api/maint/aionav/live/save", save)
        self.assertTrue(r.get_json()["success"], r.get_json())
        self.assertTrue(r.get_json()["saved"])
        self.assertEqual(self.post("/api/maint/aionav/live/save", save).status_code, 409)   # now stale
        self.assertIn("Maintenance sign-in failed", self.events)
        self.assertIn("AIO NAV config aio_nav.yaml saved", self.events)
        self.post("/api/maint/auth/lock", {})
        self.assertEqual(self.post("/api/maint/aionav/live/save", save).status_code, 401)

    def test_saved_but_restart_failed_is_not_a_success(self):
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        nav = self.ctx.cfg["nav"]["service"]
        self.ctx.store = SimpleNamespace(get=lambda k, d=None: {nav: {"pids": [123]}} if k == "services" else d)
        self.ctx.actions.run = lambda *a: {"success": False, "summary": "launcher not found"}
        version = self.c.get("/api/maint/aionav/live").get_json()["file"]["version"]
        j = self.post("/api/maint/aionav/live/save",
                      {"form": {"output_rate": 50}, "base_version": version, "restart": True}).get_json()
        self.assertTrue(j["saved"])
        self.assertFalse(j["success"])
        self.assertEqual(j["restart"], {"requested": True, "success": False})
        self.assertIn("NOT applied", j["summary"])

    def test_repeated_wrong_passwords_lock_out(self):
        for _ in range(5):
            self.post("/api/maint/auth/unlock", {"password": "wrong"})
        r = self.post("/api/maint/auth/unlock", {"password": "right-password"})
        self.assertEqual(r.status_code, 429)

    def test_simultaneous_wrong_passwords_are_checked_one_by_one(self):
        """40 attempts at once were checked 14 times in parallel (CPU); now at most 5 are."""
        from concurrent.futures import ThreadPoolExecutor
        def attempt(i):
            with self.app.test_client() as c:
                return c.post("/api/maint/auth/unlock", json={"password": f"wrong-{i}"},
                              headers={"X-Requested-With": "aio-dashboard"}).status_code
        with ThreadPoolExecutor(20) as ex:
            codes = list(ex.map(attempt, range(20)))
        self.assertEqual((codes.count(403), codes.count(429)), (5, 15))

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


class MalformedInputTest(GuardWebTest):
    """Found by the adversarial run: malformed input made these routes fail with a server error."""

    def unlock(self):
        self.post("/api/maint/auth/unlock", {"password": "right-password"})

    def raw(self, url, data):
        return self.c.post(url, data=data, headers=dict(self.h, **{"Content-Type": "application/json"}))

    def test_terminal_sizes_that_are_not_numbers(self):
        self.unlock()
        r = self.post("/api/maint/term", {"cols": "abc", "rows": [1]})
        self.assertEqual(r.status_code, 200)
        sid = r.get_json()["id"]
        self.assertEqual(self.post(f"/api/maint/term/{sid}/resize", {"cols": "abc"}).status_code, 200)
        r = self.post(f"/api/maint/term/{sid}/input", {"data": "x" * 70000})
        self.assertEqual(r.status_code, 400)
        self.assertIn("64 KB", r.get_json()["summary"])
        self.assertEqual(self.raw("/api/maint/term", b'["cols"]').status_code, 200)   # not an object: defaults

    def test_deeply_nested_yaml_is_an_error_not_a_crash(self):
        self.unlock()
        text = "a: " + "[" * 5000 + "]" * 5000 + "\n"
        r = self.post("/api/maint/aionav/live/preview", {"text": text})
        self.assertEqual(r.status_code, 200)
        self.assertIn("nested too deeply", " ".join(r.get_json()["errors"]))

    def test_too_large_body_is_refused(self):
        self.unlock()
        r = self.raw("/api/maint/aionav/live/preview", b'{"text": "' + b"x" * (2 * 1024 * 1024) + b'"}')
        self.assertEqual(r.status_code, 413)


class WholeSiteLockTest(GuardWebTest):
    """maintenance.lock: all (the default): every maintenance page and API needs the password."""
    LOCK = "all"

    # the tools-only tests do not apply here
    test_locked_until_the_right_password = test_preview_needs_no_password_and_writes_nothing = None
    test_terminal_belongs_to_the_browser_that_opened_it = None

    def test_pages_go_to_sign_in_and_apis_say_locked(self):
        r = self.c.get("/system")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login?next=/system"))
        self.assertEqual(self.c.get("/api/maint/snapshot").status_code, 401)
        self.assertEqual(self.c.get("/api/maint/events").status_code, 401)
        self.assertEqual(self.post("/api/maint/actions/restart_service", {"target": "x"}).status_code, 401)
        self.assertEqual(self.c.get("/login").status_code, 200)
        self.assertEqual(self.c.get("/static/css/dashboard.css").status_code, 200)
        self.assertEqual(self.c.get("/api/maint/auth").get_json()["scope"], "all")

    def test_signed_in_everything_opens(self):
        self.assertEqual(self.post("/api/maint/auth/unlock", {"password": "right-password"}).status_code, 200)
        self.assertEqual(self.c.get("/system").status_code, 200)
        self.assertEqual(self.c.get("/api/maint/events").status_code, 200)
        self.assertEqual(self.c.get("/login?next=/ros").headers["Location"], "/ros")   # already signed in
        self.post("/api/maint/auth/lock", {})
        self.assertEqual(self.c.get("/system").status_code, 302)

    def test_sign_in_only_returns_to_this_site(self):
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        for bad in ("//evil.example/x", "https://evil.example/", "javascript:alert(1)"):
            self.assertEqual(self.c.get("/login", query_string={"next": bad}).headers["Location"], "/", bad)

    def test_background_refresh_does_not_keep_it_open(self):
        self.post("/api/maint/auth/unlock", {"password": "right-password"})
        guard = self.ctx.guard
        token = next(iter(guard._sessions))
        ip, expires = guard._sessions[token]
        guard._sessions[token] = (ip, expires - 100)
        self.c.get("/api/maint/events")                  # a poll: does not extend
        self.assertAlmostEqual(guard._sessions[token][1], expires - 100, delta=1)
        self.c.get("/system")                            # opening a page: extends
        self.assertGreater(guard._sessions[token][1], expires - 50)


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
        # the dashboard's own environment (its bundled libraries) does not leak into the shell
        self.m.write(sid, "echo R=$AIO_DASHBOARD_TERMINAL:$(tput cols):$(echo \"$PYTHONPATH\" | grep -c aio-dashboard)\r")
        self.assertIn("R=1:90:0", self.output_until(s, "R=1"))
        self.m.resize(sid, 120, 30)
        self.m.write(sid, "exit\r")
        for _ in range(50):
            if not s.alive:
                break
            time.sleep(0.1)
        self.assertFalse(s.alive)

    def test_shell_reads_bashrc_like_a_desktop_terminal(self):
        """A login shell skips ~/.bashrc when there is no ~/.profile, and ROS is set up there."""
        sid = self.m.open("127.0.0.1")["id"]
        s = self.m.get(sid)
        rc = self.m.rcfile.read_text()
        self.assertIn("/etc/profile", rc)
        self.assertIn('"$HOME/.bashrc"', rc)
        self.m.write(sid, "case $- in *i*) echo IS=interactive;; esac\r")
        self.assertIn("IS=interactive", self.output_until(s, "IS=interactive"))

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


class StaticVersionTest(unittest.TestCase):
    """After an update the browser must not mix new pages with cached old CSS / JS."""

    def test_static_links_carry_the_file_time(self):
        try:
            from flask import url_for
            from aio_system_dashboard.web.common import create_base_app
        except ImportError as e:
            self.skipTest(str(e))
        data = copy.deepcopy(DEFAULTS)
        data["nav"]["aio_nav_config"] = ""
        app = create_base_app("t", SimpleNamespace(cfg=Config(data, None)))
        with app.test_request_context():
            a = url_for("static", filename="css/dashboard.css")
            self.assertRegex(a, r"^/static/css/dashboard\.css\?v=[0-9a-f]+$")
            self.assertNotIn("v=", url_for("static", filename="no/such/file.js"))
        r = app.test_client().get(a)
        self.assertEqual(r.status_code, 200)                 # the query does not change what is served
