"""Optional replay module: bag discovery, ros2 bag play options, the player, and the module plumbing."""

import copy
import json
import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aio_dashboard_replay import bags, player
from aio_dashboard_replay.player import OptionError, Player, Progress, command_text, play_args, validate
from aio_system_dashboard import modules
from aio_system_dashboard.config import DEFAULTS, Config

META = textwrap.dedent("""\
    rosbag2_bagfile_information:
      version: 5
      storage_identifier: sqlite3
      duration: {nanoseconds: 100000000000}
      starting_time: {nanoseconds_since_epoch: 1790132732203641313}
      message_count: 11100
      relative_file_paths: [b_0.db3]
      topics_with_message_count:
        - topic_metadata: {name: /imu, type: sensor_msgs/msg/Imu, serialization_format: cdr}
          message_count: 10000
        - topic_metadata: {name: /gnss, type: sensor_msgs/msg/NavSatFix, serialization_format: cdr}
          message_count: 100
        - topic_metadata: {name: /cam, type: sensor_msgs/msg/Image, serialization_format: cdr}
          message_count: 1000
    """)


def make_bag(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "metadata.yaml").write_text(META)
    (folder / "b_0.db3").write_bytes(b"x" * 1234)
    return folder


class BagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_metadata(self):
        b = bags.read_metadata(str(make_bag(self.root / "run1")))
        self.assertEqual(b["duration_s"], 100.0)
        self.assertEqual(b["start_ns"], 1790132732203641313)       # nanoseconds_since_epoch
        self.assertEqual(b["size"], 1234)
        self.assertEqual([t["name"] for t in b["topics"]], ["/cam", "/gnss", "/imu"])
        self.assertEqual({t["name"]: t["hz"] for t in b["topics"]}, {"/cam": 10.0, "/gnss": 1.0, "/imu": 100.0})

    def test_find_depth_hidden_and_not_inside_a_bag(self):
        make_bag(self.root / "a")
        make_bag(self.root / "a" / "nested")                 # inside a bag: not searched
        make_bag(self.root / "x" / "b")                      # depth 2
        make_bag(self.root / "x" / "y" / "c")                # depth 3: too deep
        make_bag(self.root / ".hidden" / "d")
        names = sorted(b["name"] for b in bags.find([str(self.root)], depth=2))
        self.assertEqual(names, ["a", "b"])

    def test_index_get_by_id_only(self):
        p = make_bag(self.root / "a")
        idx = bags.BagIndex([str(self.root)], 2)
        self.assertEqual(idx.get(bags.bag_id(str(p)))["name"], "a")
        self.assertIsNone(idx.get("../../etc"))


class OptionsTest(unittest.TestCase):
    BAG = {"duration_s": 100.0, "topics": [{"name": "/imu"}, {"name": "/gnss"}]}

    def test_every_humble_option_is_described(self):
        flags = {o["flag"] for o in player.OPTIONS}
        for f in ("--topics", "--rate", "--start-offset", "--loop", "--start-paused", "--clock", "--delay",
                  "--read-ahead-queue-size", "--remap", "--storage", "--qos-profile-overrides-path",
                  "--storage-config-file", "--wait-for-all-acked", "--disable-loan-message", "--log-level"):
            self.assertIn(f, flags)

    def test_defaults_give_a_plain_command(self):
        a = play_args(validate({}, self.BAG), "/b")
        self.assertEqual(a, ["--clock", "100", "--disable-keyboard-controls", "/b"])

    def test_all_options_to_arguments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as q:
            o = validate({"topics": ["/imu", "/gnss", "/imu"], "rate": 0.5, "start_offset": 12.5, "loop": True,
                          "start_paused": True, "clock": 0, "delay": 2, "read_ahead_queue_size": 50,
                          "remap": "/imu:=/imu2\n\n", "storage": "sqlite3", "qos_profile_overrides_path": q.name,
                          "wait_for_all_acked": 0, "disable_loan_message": True, "log_level": "warn"}, self.BAG)
            a = play_args(o, "/b")
        self.assertEqual(a, ["--topics", "/gnss", "/imu", "--rate", "0.5", "--start-offset", "12.5", "--loop",
                             "--start-paused", "--delay", "2", "--read-ahead-queue-size", "50",
                             "--remap", "/imu:=/imu2", "--storage", "sqlite3",
                             "--qos-profile-overrides-path", q.name, "--wait-for-all-acked", "0",
                             "--disable-loan-message", "--log-level", "warn", "--disable-keyboard-controls", "/b"])
        self.assertNotIn("--clock", a)                       # 0: no /clock

    def test_rejects(self):
        for bad in ({"rate": 0}, {"rate": "x"}, {"topics": ["/nope"]}, {"start_offset": 100},
                    {"remap": ["no-arrow"]}, {"remap": ["/a:=b c"]}, {"storage": "mcap2"}, {"evil": 1},
                    {"qos_profile_overrides_path": "/no/such/file"}, {"read_ahead_queue_size": 0}):
            with self.assertRaises(OptionError, msg=bad):
                validate(bad, self.BAG)

    def test_optional_int_empty_means_off(self):
        self.assertIsNone(validate({"wait_for_all_acked": ""}, self.BAG)["wait_for_all_acked"])

    def test_command_text_is_paste_ready(self):
        t = command_text(validate({"topics": ["/imu"]}, self.BAG), "/my bags/x",
                         {"ROS_DOMAIN_ID": "13", "ROS_LOCALHOST_ONLY": "0", "PATH": "/x"})
        self.assertEqual(t, "ROS_DOMAIN_ID=13 ROS_LOCALHOST_ONLY=0 ros2 bag play --topics /imu --clock 100 '/my bags/x'")


class ProgressTest(unittest.TestCase):
    def test_rate_pause_loop(self):
        p = Progress.new({"start_offset": 10, "delay": 2, "rate": 2.0, "start_paused": False, "loop": False}, 100, 0)
        self.assertEqual(p.position(1), 10)                  # still in the delay
        self.assertEqual(p.position(7), 20)                  # 5 s x 2
        p.change(7, paused=True)
        self.assertEqual(p.position(50), 20)
        p.change(50, paused=False, rate=1.0)
        self.assertEqual(p.position(60), 30)
        self.assertEqual(p.position(1000), 100)              # clamped at the end
        p.d["loop"] = True
        self.assertEqual(p.position(140), 10)                # 120 wraps to 20 ... (30 + 90) % 100

    def test_start_paused(self):
        p = Progress.new({"start_offset": 0, "delay": 0, "rate": 1, "start_paused": True, "loop": False}, 50, 0)
        self.assertEqual(p.position(30), 0)


class PlayerProcessTest(unittest.TestCase):
    """A stand-in `ros2` program: the real start / stop / finish / fail paths without ROS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        (d / "bin").mkdir()
        self.fake = d / "bin" / "ros2"
        self.setup = d / "setup.bash"
        self.setup.write_text("export FAKE_ROS=1\n")
        self.env = {"PATH": f"{d / 'bin'}:/usr/bin:/bin", "ROS_DOMAIN_ID": "77"}
        self.p = Player(d / "state.json", d / "play.log", d / "presets.json")
        self.bag = {"id": "abc", "name": "b", "path": str(d), "duration_s": 100.0}
        self.addCleanup(lambda: self.p.stop(grace_s=0.5))

    def fake_ros2(self, body: str):
        self.fake.write_text("#!/bin/sh\n" + body)
        self.fake.chmod(0o755)

    def test_start_status_stop(self):
        self.fake_ros2('echo "domain $ROS_DOMAIN_ID ros $FAKE_ROS args $*"\nsleep 60\n')
        r = self.p.start(self.bag, validate({}, {"duration_s": 100, "topics": []}), str(self.setup), self.env, "/")
        self.assertTrue(r["success"], r)
        st = self.p.status()
        self.assertEqual(st["status"], "playing")
        self.assertIn("domain 77 ros 1 args bag play --clock 100 --disable-keyboard-controls", self.p.log_tail())
        again = Player(self.p.state_file, self.p.log_file, self.p.presets_file)   # a restarted dashboard
        self.assertTrue(again.running())
        self.assertFalse(self.p.start(self.bag, validate({}, {"duration_s": 100, "topics": []}),
                                      str(self.setup), self.env, "/")["success"])   # one at a time
        r = self.p.stop(grace_s=2)
        self.assertTrue(r["success"])
        self.assertEqual(self.p.status()["status"], "stopped")

    def test_finishes_by_itself(self):
        self.fake_ros2("sleep 2\nexit 0\n")
        self.assertTrue(self.p.start(self.bag, validate({}, {"duration_s": 100, "topics": []}),
                                     str(self.setup), self.env, "/")["success"])
        time.sleep(1.5)
        self.assertEqual(self.p.status()["status"], "finished")

    def test_fails_at_once(self):
        self.fake_ros2('echo "Exception: bad bag" >&2\nexit 1\n')
        r = self.p.start(self.bag, validate({}, {"duration_s": 100, "topics": []}), str(self.setup), self.env, "/")
        self.assertFalse(r["success"])
        self.assertIn("bad bag", r["summary"])
        self.assertEqual(self.p.status()["status"], "failed")

    def test_presets_and_last(self):
        opts = validate({"rate": 2}, {"duration_s": 100, "topics": []})
        self.assertTrue(self.p.save_preset("fast", self.bag, opts)["success"])
        self.assertFalse(self.p.save_preset(" ", self.bag, opts)["success"])
        self.p.remember(self.bag, opts)
        got = self.p.presets()
        self.assertEqual(got["presets"]["fast"]["options"]["rate"], 2)
        self.assertEqual(got["last"]["abc"]["rate"], 2)
        self.assertTrue(self.p.delete_preset("fast")["success"])
        self.assertFalse(self.p.delete_preset("fast")["success"])


class PreconditionsTest(unittest.TestCase):
    @staticmethod
    def cfg(mode):
        data = {"ros": {"modes": {"live": {"label": "Live"}, "bag": {"label": "Bag replay"}}},
                "services": {"drivers": {"label": "Sensor drivers", "user_unit": "d.service"}}}
        return type("C", (), {
            "ros_mode": lambda s: mode, "__getitem__": lambda s, k: data[k],
            "ros_mode_info": lambda s: {"modes": [{"name": "live", "domain_id": 10},
                                                  {"name": "bag", "domain_id": 13}]}})()

    def test_mode_must_be_bag(self):
        items, ok = player.preconditions(self.cfg("live"), {}, playing=False)
        self.assertFalse(ok)
        by = {i["key"]: i for i in items}
        self.assertEqual(by["mode"]["fix"], {"kind": "mode", "mode": "bag", "label": "Switch to Bag replay"})
        self.assertTrue(player.preconditions(self.cfg("bag"), {}, playing=False)[1])
        self.assertFalse(player.preconditions(self.cfg("bag"), {}, playing=True)[1])

    def test_drivers_only_matter_in_the_bag_domain(self):
        run = {"drivers": {"state": "running"}}
        # the usual case: drivers in Live's domain 10, the bag in 13 -> they never see each other
        items, ok = player.preconditions(self.cfg("bag"), run, False, driver_domains=lambda k: {10})
        self.assertTrue(ok)
        self.assertIn("separate from the bag (domain 13)", items[1]["text"])
        self.assertIsNone(items[1]["fix"])
        # drivers publishing into the bag's domain would mix with the recording
        items, ok = player.preconditions(self.cfg("bag"), run, False, driver_domains=lambda k: {13})
        self.assertFalse(ok)
        self.assertEqual(items[1]["fix"]["action"], "stop_driver")
        # domain unreadable: be safe
        self.assertFalse(player.preconditions(self.cfg("bag"), run, False, driver_domains=lambda k: None)[1])
        # stopped drivers are always fine
        self.assertTrue(player.preconditions(self.cfg("bag"), {"drivers": {"state": "stopped"}}, False)[1])

    def test_process_domain(self):
        import subprocess
        self.assertIsNone(player.process_domain(999999999))
        for env, want in (({"ROS_DOMAIN_ID": "13"}, 13), ({}, 0)):        # unset means domain 0
            proc = subprocess.Popen(["sleep", "5"], env=dict(env, PATH="/usr/bin:/bin"))
            try:
                time.sleep(0.1)
                self.assertEqual(player.process_domain(proc.pid), want)
            finally:
                proc.kill(); proc.wait()


class ModulePlumbingTest(unittest.TestCase):
    def test_installed_and_switch(self):
        self.assertTrue(modules.installed("replay"))
        self.assertFalse(modules.installed("nope"))
        self.assertTrue(modules.enabled({}, "replay"))
        self.assertFalse(modules.enabled({"modules": {"replay": {"enabled": False}}}, "replay"))

    def test_without_the_module_the_machine_runs_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "ros_mode.json"
            state.write_text(json.dumps({"mode": "bag"}))
            data = copy.deepcopy(DEFAULTS)
            data["ros"]["mode"] = "live"
            with mock.patch.object(Config, "ros_mode_file", lambda _s: state):
                self.assertEqual(Config(copy.deepcopy(data), None).ros_mode(), "bag")
                with mock.patch.object(modules, "installed", lambda name: False):
                    self.assertEqual(Config(copy.deepcopy(data), None).ros_mode(), "live")
                data["modules"] = {"replay": {"enabled": False}}
                self.assertEqual(Config(copy.deepcopy(data), None).ros_mode(), "live")

    def test_maintenance_sidebar_and_routes(self):
        try:
            from aio_system_dashboard.web.maintenance import create_maintenance_app
        except ImportError as e:
            self.skipTest(str(e))
        import aio_dashboard_replay
        with tempfile.TemporaryDirectory() as tmp:
            data = copy.deepcopy(DEFAULTS)
            data["modules"] = {"replay": {"bag_roots": [tmp]}}
            data["maintenance"]["lock"] = "tools"          # module plumbing here, not the password
            make_bag(Path(tmp) / "run1")
            cfg = Config(data, None)
            with mock.patch.object(Config, "state_file", lambda _s, n: Path(tmp) / "state" / n):
                for mods, has in (([], False), ([aio_dashboard_replay], True)):
                    ctx = SimpleNamespace(cfg=cfg, modules=mods, store=SimpleNamespace(get=lambda k, d=None: d),
                                          actions=SimpleNamespace(describe=lambda: []),
                                          preview=SimpleNamespace(describe=lambda: {}), events=None)
                    c = create_maintenance_app(ctx).test_client()
                    self.assertEqual(c.get("/replay").status_code, 200 if has else 404)
                    self.assertEqual("Replay" in c.get("/system").get_data(as_text=True), has)
                    self.assertEqual(c.post("/api/maint/ros-mode", json={"mode": "bag"},
                                            headers={"X-Requested-With": "aio-dashboard"}).status_code, 404)
                    if has:
                        j = c.get("/api/replay/bags").get_json()
                        self.assertEqual([b["name"] for b in j["bags"]], ["run1"])
                        r = c.post("/api/replay/play", json={"bag": "nope", "options": {}},
                                   headers={"X-Requested-With": "aio-dashboard"})
                        self.assertEqual(r.status_code, 404)
                        r = c.post("/api/replay/stop", json={})               # no X-Requested-With
                        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
