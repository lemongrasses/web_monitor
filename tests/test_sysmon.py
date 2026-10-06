import copy
import gzip
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from aio_sysmon import config as cfgmod, daemon, kmsg, report, sources
from aio_sysmon.probes import LatencyProbe
from aio_sysmon.ring import RingFile, SLOT, read_ring
from aio_sysmon.store import CRITICAL, LOW, OK, Store
from aio_sysmon.triggers import TriggerEngine


def small_cfg(d, **over):
    cfg = copy.deepcopy(cfgmod.DEFAULTS)
    cfg.update(dir=d, kmsg=False, min_free_mb=2048, ring_mb=1, **over)
    return cfg


class TmpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name


class RingTest(TmpTest):
    def test_fixed_size_wraps_and_keeps_the_newest(self):
        p = os.path.join(self.dir, "r.bin")
        r = RingFile(p, 64 * SLOT)
        for i in range(100):
            r.append({"t": 1000 + i})
        r.sync()
        recs = r.read_all()
        self.assertEqual((len(recs), recs[0]["t"], recs[-1]["t"]), (64, 1036, 1099))
        self.assertEqual(os.path.getsize(p), 64 * SLOT)         # never grows

    def test_restart_continues_in_order_and_oversize_is_refused(self):
        p = os.path.join(self.dir, "r.bin")
        r = RingFile(p, 64 * SLOT)
        for i in range(10):
            r.append({"t": i + 1})
        r.close()
        r2 = RingFile(p, 64 * SLOT)
        r2.append({"t": 100})
        self.assertEqual([x["t"] for x in r2.read_all()][-2:], [10, 100])
        self.assertEqual(r2.append({"t": 5, "junk": "x" * 600}), 0)

    def test_a_damaged_slot_loses_only_that_record(self):
        p = os.path.join(self.dir, "r.bin")
        r = RingFile(p, 64 * SLOT)
        for i in range(5):
            r.append({"t": i + 1})
        r.sync()
        os.pwrite(r.fd, b"{\"t\": 3, garbage", 2 * SLOT)         # a torn write in slot 2
        self.assertEqual([x["t"] for x in read_ring(p)], [1, 2, 4, 5])


class StoreBudgetTest(TmpTest):
    def make(self, free=50000, **over):
        cfg = small_cfg(self.dir, **over)
        self.free = free
        return Store(cfg, free_mb=lambda _p: self.free)

    def test_disk_guard_levels_and_what_they_stop(self):
        s = self.make(free=50000)
        self.assertEqual(s.disk_level(), OK)
        self.assertIsNotNone(s.write_snapshot("t", {"a": 1}))
        self.free = 1000                                          # below min_free (2048)
        self.assertEqual(s.disk_level(), LOW)
        self.assertIsNone(s.write_snapshot("t", {"a": 1}))          # no snapshots when space is short
        self.assertIsNotNone(s.write_sample({"t": time.time()}))    # but the daily log continues
        self.free = 100                                           # below critical (512)
        self.assertEqual(s.disk_level(), CRITICAL)
        self.assertIsNone(s.write_sample({"t": time.time()}))       # almost nothing is written
        s.write_event("anomaly", {"x": 1})                          # tiny events still are

    def test_snapshots_per_day_are_capped(self):
        s = self.make(max_snapshots_per_day=3)
        results = [s.write_snapshot("k", {"i": i}) for i in range(6)]
        self.assertEqual(sum(r is not None for r in results), 3)

    def test_budget_compresses_old_days_and_trims_snapshots_before_samples(self):
        s = self.make(max_total_mb=1, snapshots_share=0.5, retention_days=30)
        old = os.path.join(self.dir, "samples", "samples-20200101.jsonl")
        with open(old, "w") as f:
            f.write(("x" * 1000 + "\n") * 2000)
        for i in range(30):                                       # big, incompressible-ish snapshots
            p = os.path.join(self.dir, "snapshots", f"snap-2026{i:04d}-000000-t.json.gz")
            with open(p, "wb") as f:
                f.write(os.urandom(40000))
            os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        stats = s.enforce_budget()
        self.assertTrue(os.path.exists(old + ".gz") and not os.path.exists(old))   # yesterday's file compressed
        snaps = os.listdir(os.path.join(self.dir, "snapshots"))
        self.assertLessEqual(sum(os.path.getsize(os.path.join(self.dir, "snapshots", f)) for f in snaps), 0.5 * 1048576 + 40000)
        self.assertIn("snap-20260029-000000-t.json.gz", snaps)      # the newest survive
        self.assertGreater(stats["deleted"], 0)

    def test_total_cap_gives_up_old_samples_when_still_over(self):
        s = self.make(max_total_mb=1)
        for d in range(5):
            with open(os.path.join(self.dir, "samples", f"samples-2020010{d + 1}.jsonl.gz"), "wb") as f:
                f.write(os.urandom(400000))
        s.enforce_budget()
        self.assertLessEqual(s.used_bytes(), 1048576)

    def test_retention_removes_files_older_than_the_limit(self):
        s = self.make(retention_days=7, max_total_mb=500)
        p = os.path.join(self.dir, "samples", "samples-20200101.jsonl.gz")
        with gzip.open(p, "wt") as f:
            f.write("{}\n")
        os.utime(p, (time.time() - 10 * 86400,) * 2)
        s.enforce_budget()
        self.assertFalse(os.path.exists(p))

    def test_last_known_is_replaced_atomically(self):
        s = self.make()
        s.write_last_known({"t": 1, "b": 5})
        s.write_last_known({"t": 2, "b": 6})
        self.assertEqual(json.load(open(os.path.join(self.dir, "last_known.json")))["t"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "last_known.json.tmp")))


class ThresholdTest(unittest.TestCase):
    def test_limits_scale_with_the_machine(self):
        big = cfgmod.derive_limits({"cores": 8, "mem_total_mb": 15655, "swap_total_mb": 16000,
                                    "disk_total_mb": 137000, "temp_trips_c": [70, 95, 99]})
        small = cfgmod.derive_limits({"cores": 2, "mem_total_mb": 2048, "swap_total_mb": 0,
                                      "disk_total_mb": 16000, "temp_trips_c": []})
        self.assertEqual((big["load1"], small["load1"]), (12.0, 3.0))
        self.assertGreater(big["mem_avail_mb"], small["mem_avail_mb"])
        self.assertEqual(small["mem_avail_mb"], 512)                  # a floor for small machines
        self.assertEqual(big["temp_c"], 85.0)                         # 10 below the first hot trip (95)
        self.assertEqual(small["temp_c"], 85.0)                       # no trips known: default
        self.assertIsNone(small["swap_used_pct"])                     # no swap: no swap rule
        self.assertGreater(big["disk_free_mb"], small["disk_free_mb"])

    def test_overrides_win(self):
        lim = cfgmod.derive_limits({"cores": 8}, {"iowait_pct": 10, "not_a_limit": 1})
        self.assertEqual(lim["iowait_pct"], 10)
        self.assertNotIn("not_a_limit", lim)


class TriggerTest(unittest.TestCase):
    def engine(self, cooldown=120):
        lim = cfgmod.derive_limits({"cores": 8, "mem_total_mb": 16000, "swap_total_mb": 16000,
                                    "disk_total_mb": 137000, "temp_trips_c": [95]})
        return TriggerEngine(lim, cooldown)

    def feed(self, e, n, **kv):
        fired = []
        for i in range(n):
            self.t = getattr(self, "t", 1000.0) + 1
            fired += e.evaluate(dict({"t": self.t}, **kv))
        return [f["kind"] for f in fired]

    def test_a_single_blip_is_ignored_and_a_sustained_condition_fires_once(self):
        e = self.engine()
        self.assertEqual(self.feed(e, 1, w=60), [])                       # one second of iowait: ignored
        e = self.engine()
        self.assertEqual(self.feed(e, 5, w=60), ["iowait"])                 # sustained: fires once (cool-down)

    def test_stall_and_slow_write_fire_at_once(self):
        e = self.engine()
        self.assertEqual(self.feed(e, 1, sc=1500), ["stall"])
        e = self.engine()
        self.assertEqual(self.feed(e, 1, fs=2500), ["slow_disk_write"])

    def test_memory_swap_and_heat(self):
        e = self.engine()
        self.assertEqual(self.feed(e, 2, a=300), ["memory_low"])
        e = self.engine()
        self.assertEqual(self.feed(e, 3, s=6000, swap_total=16000), ["swap_high"])
        e = self.engine()
        self.assertEqual(self.feed(e, 3, T=90), ["hot"])
        e = self.engine()
        self.assertEqual(self.feed(e, 5, T=60, a=9000, l=1.0, b=30), [])    # a healthy machine is silent

    def test_cooldown_then_fires_again(self):
        e = self.engine(cooldown=10)
        self.assertEqual(self.feed(e, 5, sc=1500), ["stall"])
        self.assertEqual(self.feed(e, 12, sc=1500), ["stall"])               # after the cool-down

    def test_power_dip_is_relative_to_the_recent_median(self):
        e = self.engine()
        self.assertEqual(self.feed(e, 70, v=19000), [])
        self.assertEqual(self.feed(e, 1, v=19050), [])
        self.assertEqual(self.feed(e, 1, v=15500), ["power_dip"])           # 18% below
        e2 = self.engine()
        self.feed(e2, 70, v=12000)
        self.assertEqual(self.feed(e2, 1, v=11800), [])                      # a 12 V supply is judged on its own level

    def test_disk_space_rule_scales_and_is_rate_limited(self):
        e = self.engine()
        self.assertEqual(self.feed(e, 5, f=3000), ["disk_space"])
        self.assertEqual(self.feed(e, 30, f=3000), [])                       # once an hour, not every minute


class ProbeTest(unittest.TestCase):
    def test_take_returns_the_worst_lateness_and_resets(self):
        p = LatencyProbe()
        for v in (3.0, 950.0, 12.0, 250.0):
            p.record(v)
        self.assertEqual(p.take(), (950.0, 2))                              # 2 wake-ups later than 200 ms
        self.assertEqual(p.take(), (0.0, 0))


class KmsgTest(unittest.TestCase):
    def test_only_real_problems_are_recorded(self):
        seen = []
        r = kmsg.KmsgReader(lambda k, d: seen.append(d["msg"]))
        for line in ["6,1,100,-;usb 1-1: new device", "3,2,200,-;nvme nvme0: I/O 152 QID 8 timeout, completion polled",
                     "4,3,300,-;Out of memory: Killed process 123 (x)", " SUBSYSTEM=pci",
                     "6,4,400,-;thermal-trip-event cpu-throttle-alert: cooling device registered", "garbage"]:
            r.handle(line)
        self.assertEqual(len(seen), 2)
        self.assertIn("timeout", seen[0])
        self.assertEqual(len(r.recent), 4)                                  # everything stays in memory for snapshots

    def test_a_message_storm_is_capped(self):
        seen = []
        r = kmsg.KmsgReader(lambda k, d: seen.append(d), max_per_min=10)
        for i in range(500):
            word = chr(97 + i // 26 % 26) + chr(97 + i % 26)          # 500 different messages (digits do not count)
            r.handle(f"3,{i},{i},-;nvme error {word}")
        self.assertEqual(len(seen), 10)


class UncleanAnalysisTest(unittest.TestCase):
    def ring(self, **last):
        recs = [{"t": 1000 + i, "T": 50, "l": 1.0, "w": 0, "a": 9000, "s": 50, "sc": 1, "fs": 2, "k": 0, "v": 19100, "f": 20000}
                for i in range(60)]
        for r in recs[-10:]:
            r.update(last)
        return recs

    def verdict(self, boot="b2", **last):
        return daemon.analyze_unclean({"boot_id": "b1"}, {"t": 1059}, self.ring(**last), boot, 5000, [])

    def test_normal_readings_mean_a_sudden_stop(self):
        v = self.verdict()
        self.assertFalse(v["same_boot"])
        self.assertIn("sudden power loss", v["summary"])

    def test_each_cause_is_named(self):
        self.assertIn("input voltage", self.verdict(v=15000)["hint"])
        self.assertIn("overheating", self.verdict(T=92)["hint"])
        self.assertIn("memory", self.verdict(a=200)["hint"])
        self.assertIn("disk", self.verdict(w=70)["hint"])
        self.assertIn("stalling", self.verdict(sc=2500)["hint"])

    def test_same_boot_means_only_the_recorder_died(self):
        v = self.verdict(boot="b1")
        self.assertTrue(v["same_boot"])
        self.assertIn("recorder was killed", v["summary"])


class DaemonTest(TmpTest):
    def make(self):
        cfg = small_cfg(self.dir, interval_s=1.0)
        return daemon.Daemon(cfg)

    def test_a_clean_stop_leaves_no_verdict_and_an_unclean_one_does(self):
        d1 = self.make()
        self.assertIsNone(d1.boot_analysis())                  # first run ever
        for _ in range(3):
            d1.tick()
        d1.shutdown()                                          # clean
        d2 = self.make()
        self.assertIsNone(d2.boot_analysis())                  # previous run was clean
        for _ in range(4):
            d2.tick()
        d2.ring.append_many(d2._pending)
        d2.ring.sync()
        # d2 is "killed": no shutdown, no clean marker. The next start finds out.
        d3 = self.make()
        d3.boot_id = "another-boot-id"
        v = d3.boot_analysis()
        self.assertIsNotNone(v)
        self.assertFalse(v["same_boot"])
        kinds = [e["kind"] for e in report.events(self.dir, 0, time.time() + 5)]
        self.assertIn("unclean_stop", kinds)
        self.assertTrue(report.last_unclean(self.dir))
        self.assertTrue(any(f.endswith("postmortem.json.gz") for f in os.listdir(os.path.join(self.dir, "snapshots"))))

    def test_a_trigger_writes_an_event_a_snapshot_and_starts_a_burst(self):
        d = self.make()
        d.boot_analysis()
        with mock.patch.object(daemon.time, "sleep"):
            d.probe.record(2500.0)                             # a 2.5 s stall was just measured
            d.tick()
        evs = report.events(self.dir, 0, time.time() + 5)
        self.assertIn("stall", [e.get("trigger") for e in evs if e["kind"] == "anomaly"])
        snaps = os.listdir(os.path.join(self.dir, "snapshots"))
        self.assertTrue(any("stall" in f for f in snaps))
        self.assertGreater(d.burst_until, d.mono())            # detailed sampling for a while
        text = report.describe_snapshot(os.path.join(self.dir, "snapshots", [f for f in snaps if "stall" in f][0]))
        self.assertIn("trigger: stall", text)
        self.assertIn("top CPU", text)
        d.shutdown()

    def test_low_disk_space_means_no_snapshot_and_no_burst(self):
        d = self.make()
        d.boot_analysis()
        d.store._free_mb = lambda _p: 1000.0                   # below min_free: LOW
        with mock.patch.object(daemon.time, "sleep"):
            d.probe.record(2500.0)
            d.tick()
        self.assertEqual(os.listdir(os.path.join(self.dir, "snapshots")), [])
        self.assertLessEqual(d.burst_until, d.mono())
        self.assertIn("stall", [e.get("trigger") for e in report.events(self.dir, 0, time.time() + 5) if e["kind"] == "anomaly"])
        d.shutdown()

    def test_a_stalled_recorder_logs_the_gap(self):
        d = self.make()
        d.boot_analysis()
        d.tick()
        d._last_tick -= 20.0                                    # the loop did not run for 20 s
        d.tick()
        self.assertIn("tick_gap", [e["kind"] for e in report.events(self.dir, 0, time.time() + 5)])
        d.shutdown()


class ReportTest(TmpTest):
    def test_hourly_summary(self):
        base = 1_791_300_000.0
        ss = [{"t": base + i, "cpu": {"busy": 10 + i}, "ld": [1 + i / 10, 1, 1], "mem": {"avail_mb": 9000 - i, "swap_used_mb": 50},
               "tmp": {"cpu": 50 + i}, "pw": {"VDD_IN": {"mv": 19000 - i}}, "dsk": {"u": i, "free_mb": 20000}} for i in range(5)]
        row = report.hourly(ss)[0]
        self.assertEqual((row["n"], row["cpu_max"], row["mem_min"], row["temp_max"], row["vin_min"]), (5, 14, 8996, 54, 18996))


if __name__ == "__main__":
    unittest.main()


class CapacityDefaultsTest(unittest.TestCase):
    def test_budget_follows_the_disk_size(self):
        self.assertEqual(cfgmod.default_max_total_mb({"disk_total_mb": 137000}), 250)      # big disk: the default cap
        self.assertEqual(cfgmod.default_max_total_mb({"disk_total_mb": 16000}), 60)       # small disk: a floor
        self.assertEqual(cfgmod.default_max_total_mb({"disk_total_mb": 50000}), 150)
        self.assertGreater(cfgmod.default_min_free_mb({"disk_total_mb": 137000}), 2048)


class SnapshotPermissionTest(TmpTest):
    def test_snapshots_are_not_world_readable(self):
        s = Store(small_cfg(self.dir), free_mb=lambda _p: 50000)
        path = s.write_snapshot("t", {"cmd": "secret --token abc"})
        self.assertEqual(os.stat(path).st_mode & 0o007, 0)            # no access for others


class CommandLineTest(unittest.TestCase):
    """The exact command lines used by the service file and by people."""

    def test_the_service_command_line_parses(self):
        import re
        from aio_sysmon.__main__ import build_parser
        unit = open(os.path.join(os.path.dirname(__file__), "..", "deploy", "aio-sysmon.service")).read()
        exec_line = re.search(r"^ExecStart=/usr/bin/python3 -m aio_sysmon (.*)$", unit, re.M).group(1)
        args = build_parser().parse_args(exec_line.replace("@PREFIX@", "/opt/x").split())
        self.assertEqual((args.cmd, args.config), ("run", "/opt/x/config/sysmon.yaml"))

    def test_options_work_before_and_after_the_command(self):
        from aio_sysmon.__main__ import build_parser
        p = build_parser()
        self.assertEqual(p.parse_args(["--config", "/a", "status"]).config, "/a")
        self.assertEqual(p.parse_args(["status", "--config", "/b", "--dir", "/d"]).config, "/b")
        self.assertEqual(p.parse_args(["status"]).dir, None)
        self.assertEqual(p.parse_args(["report", "--hours", "2"]).hours, 2.0)


class KmsgRepeatTest(unittest.TestCase):
    def reader(self, **kw):
        self.seen = []
        self.t = 0.0
        return kmsg.KmsgReader(lambda k, d: self.seen.append((k, d)), clock=lambda: self.t, **kw)

    def test_only_the_first_line_of_a_record_is_kept(self):
        r = self.reader()
        r.handle("3,1,100,-;nvme timeout\n SUBSYSTEM=pci\n DEVICE=+pci:0000")
        self.assertEqual(self.seen[0][1]["msg"], "nvme timeout")

    def test_the_same_message_is_logged_once_and_counted(self):
        r = self.reader()
        for i in range(50):
            self.t += 1
            r.handle(f"4,{i + 1},{i},-;systemd-fstab-generator[{100 + i}]: Failed to create unit file, duplicate entry")
        self.assertEqual(sum(1 for k, _ in self.seen if k == "kernel"), 1)
        r.flush_repeats()
        rep = [d for k, d in self.seen if k == "kernel_repeats"]
        self.assertEqual(rep[0]["count"], 49)

    def test_it_logs_again_after_the_window_and_reports_the_count(self):
        r = self.reader(repeat_window_s=60)
        for i in range(5):
            r.handle(f"3,{i + 1},{i},-;disk error {i}")
        self.t += 120
        r.handle("3,10,10,-;disk error 99")
        kinds = [k for k, _ in self.seen]
        self.assertEqual(kinds, ["kernel", "kernel_repeats", "kernel"])

    def test_restart_resumes_after_the_last_sequence_number(self):
        r = self.reader(min_seq=40)
        r.handle("3,39,1,-;old problem")
        r.handle("3,40,2,-;also old")
        r.handle("3,41,3,-;new problem")
        self.assertEqual([d["msg"] for k, d in self.seen], ["new problem"])
        self.assertEqual(r.last_seq, 41)
