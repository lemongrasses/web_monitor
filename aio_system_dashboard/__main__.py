"""AIO System Dashboard entry point.

One process, shared state, two HTTP servers:
  Product UI      :8080  (Overview / Navigation / Data)
  Maintenance UI  :8081  (System / ROS 2 / Camera / LiDAR / Network / Diagnostics)
"""

import argparse
import logging
import signal
import os
import threading
import time


from .actions.autostart import StartupCoordinator
from .actions.driver_watchdog import DriverWatchdog
from .collectors.gnss_time import ChronyShm, GnssClockFeeder
from . import sdnotify
from .actions.registry import ActionRegistry
from .collectors.fake import FakeState
from .actions import process_control
from .collectors.dso_watchdog import DsoWatchdog
from .collectors.gnss_monitor import GnssMonitor
from .collectors.speed import SpeedMonitor
from .collectors.nav_udp import NavUdpCollector
from .collectors.ros_guard import RosIsolationGuard
from .collectors.network import NetworkCollector
from .collectors.ros2 import Ros2Collector
from .collectors.services import ServicesCollector
from .collectors.status_udp import StatusUdpPublisher
from .collectors.system import SystemCollector
from . import modules
from .config import fallback_config, load_config_safely, remember_good, resolve_path
from .data_access.files import DataRoots
from .media.tap import PreviewTap
from .state import still
from .state.health import HealthEngine
from .state.store import EventLog, StateStore
from .web.server import BoundedWSGIServer
from .web.maintenance import create_maintenance_app
from .web.product import create_product_app

logger = logging.getLogger("aio_dashboard")


class DashboardContext:
    """Everything the web apps need; collectors write, web apps read."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.store = StateStore()
        ev = cfg["events"]
        self.events = EventLog(resolve_path(ev["log_file"]) if ev.get("log_file") else None,
                               ev["max_memory"])
        self.fake = FakeState(resolve_path(cfg["fake"]["state_file"])) if cfg.fake else None
        self.nav = NavUdpCollector(
            cfg["nav"]["udp_bind"], cfg["nav"]["trajectory"],
            on_session_reset=lambda s, reason: self.events.add(
                "info", "nav", "New NAV session", reason))
        self.system = SystemCollector(self.store, cfg)
        self.services = ServicesCollector(self.store, cfg, self.fake)
        self.network = NetworkCollector(self.store, cfg, self.fake)
        self.preview = PreviewTap(cfg, fake=cfg.fake, fake_state=self.fake)
        dso = cfg["dso_watchdog"]["service"]
        self.dso_watchdog = DsoWatchdog(
            cfg, self.store, self.events,
            restart=lambda: self.actions.run("restart_process", dso),
            start=lambda: self.actions.run("start_process", dso),
            process_running=lambda: bool(process_control.running_pids(cfg, dso)),
            wanted=lambda: process_control.wanted(cfg, dso),
            adopt=lambda: process_control.adopt(cfg, dso),
            memory_mb=lambda: process_control.memory_mb(cfg, dso),
            total_memory_mb=process_control.total_memory_mb())
        self.gnss = GnssMonitor(cfg, self.store)
        self.speed = SpeedMonitor(cfg, self.store)
        gt = cfg["gnss_time"]
        self.gnss_clock = (GnssClockFeeder(float(gt["latency_ms"]) / 1000.0, ChronyShm(int(gt["shm_unit"])))
                           if gt["enabled"] and not cfg.fake else None)
        drivers = next((k for k, s in cfg["services"].items() if s.get("user_unit")), None)
        self.guard = RosIsolationGuard(
            cfg, self.events,
            driver_pids=lambda: process_control.running_pids(cfg, drivers) if drivers else [],
            restart=self.request_restart, state_file=cfg.state_file("ros_self_restart.json"))
        self.ros = Ros2Collector(self.store, cfg, self.fake, self.preview,
                                 watchers=[self.dso_watchdog, self.gnss, self.speed] if not cfg.fake else [],
                                 guard=None if cfg.fake else self.guard, clock_feeder=self.gnss_clock)
        self.speed.window_open = self.ros.sampling_window_open     # listen together with the IMU
        self.health = HealthEngine(cfg, self.store, self.nav, self.events)
        self.still_limits = still.StillLimits(cfg, cfg.state_file("still_limits.json"), self.events)
        self.health.still_limits = self.still_limits
        self.still_calibration = still.StillCalibration(self.health.motion_inputs, self.still_limits)
        self.actions = ActionRegistry(cfg, self.store, self.events, self.network.check_device,
                                      self.fake)
        self.data = DataRoots(cfg["data"]["roots"])
        self.ros_env_at_start = dict(cfg.ros_env())   # this process's own ROS connection uses these
        self.actions.after_run = self._after_action
        # Start-up coordinator (autostart) and the 0x05 status packet; both off unless configured.
        self.autostart = StartupCoordinator(cfg, self.store, self.actions, self.events,
                                            on_start=lambda: self.nav.clear("AIO NAV automatic start"))
        self.health.autostart_status = self.autostart.status
        self.health.clock_status = self.gnss_clock.status if self.gnss_clock else None
        self.driver_watchdog = DriverWatchdog(cfg, self.store, self.actions, self.events)
        self.status_udp = StatusUdpPublisher(cfg, self.store)
        self.health.status_publisher = self.status_udp
        self.modules = modules.load(cfg)      # optional modules (installed with --with-<name>)
        self.maint_pages = []                 # sidebar of the Maintenance app, filled when it is built
        self._workers = [self.nav, self.system, self.services, self.network, self.ros, self.health,
                         self.autostart, self.driver_watchdog]

    def alive(self, max_age_s: float = 20.0) -> bool:
        """The health loop (readiness, 0x05) and the start-up coordinator have run recently."""
        loops = [self.health.last_step] + ([self.autostart.last_tick] if self.autostart.enabled else [])
        return sdnotify.loops_alive(loops, time.monotonic(), max_age_s)

    def _after_action(self, action_id: str, target: str, result: dict) -> None:
        """After AIO NAV was started with a freshly read config file: follow what changed in it."""
        coordinator = getattr(self, "autostart", None)
        if coordinator is not None:
            coordinator.on_action(action_id, target)    # an operator Stop holds the automatic start
        drivers = getattr(self, "driver_watchdog", None)
        if drivers is not None:
            drivers.on_action(action_id, target)        # an operator Stop of the drivers is kept
        if action_id not in ("start_process", "restart_process") or \
                target not in (process_control.GROUP, self.cfg["nav"]["service"]):
            return
        self.data = DataRoots(self.cfg["data"]["roots"])          # its log folder may have moved
        now = dict(self.cfg.ros_env())
        if now != self.ros_env_at_start:
            # The dashboard's own ROS connection cannot change domain while it runs: restart it
            # (AIO NAV and DSO keep running) so it sees the same domain as AIO NAV again.
            old = ", ".join(f"{k}={v}" for k, v in sorted(self.ros_env_at_start.items())) or "unset"
            new = ", ".join(f"{k}={v}" for k, v in sorted(now.items())) or "unset"
            self.events.add("warning", "system", "ROS settings in the AIO NAV config changed",
                            f"{old} -> {new}; dashboard restarting to follow")
            self.request_restart()

    def request_restart(self):
        """Stop this process shortly (systemd Restart=always brings it back with the new settings)."""
        if self.cfg.fake:
            logger.info("fake mode: restart requested, not restarting")
            return
        threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()

    def start(self):
        self.dso_watchdog._publish()
        for w in self._workers:
            w.start()

    def stop(self):
        if getattr(self, "terminals", None) is not None:
            self.terminals.shutdown()                  # no shells are left behind
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.join()
        self.status_udp.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aio_system_dashboard")
    parser.add_argument("--config", default="config/dashboard.yaml",
                        help="YAML config (relative paths resolve against the project root)")
    parser.add_argument("--fake", action="store_true",
                        help="use dev/fake_state.json for services, network and ROS")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    # A settings file with an error must not stop the fully automatic system: fall back to the
    # settings that last started the dashboard (or the defaults) and say so.
    cfg, problem = load_config_safely(args.config, force_fake=args.fake)
    try:
        ctx = DashboardContext(cfg)
    except Exception as e:                           # a value of the wrong type, say
        if problem:
            raise
        logger.exception("cannot start with %s", args.config)
        cfg, which = fallback_config(force_fake=args.fake)
        problem = f"settings file {args.config} unusable ({e}); running with {which}"
        ctx = DashboardContext(cfg)
    for key, value in cfg.ros_env().items():  # before rclpy starts
        os.environ[key] = value
    if cfg.ros_env():
        logger.info("ROS environment from config: %s", cfg.ros_env())
    info = cfg.ros_mode_info()
    ros_text = f"ROS {info['label']}" + (f" (domain {info['domain_id']})" if info["domain_id"] is not None else "")
    ctx.events.add("info", "system", "Dashboard started", "fake mode" if cfg.fake else ros_text)
    if problem:
        ctx.events.add("fault", "system", "Settings file has an error", problem)
    if cfg.problems:
        ctx.events.add("fault", "system", "Settings with an invalid value: defaults used",
                       "; ".join(cfg.problems))
    ctx.start()

    servers = []
    for key, factory in (("product", create_product_app), ("maintenance", create_maintenance_app)):
        host, port = cfg[key]["host"], int(cfg[key]["port"])
        try:
            srv = BoundedWSGIServer(host, port, factory(ctx))   # bounded: idle timeout, connection caps
        except (OSError, SystemExit) as e:  # werkzeug exits on "address in use"
            logger.error("cannot start %s UI on %s:%d: %s", key, host, port, e)
            for s in servers:
                s.shutdown()
            ctx.stop()
            return 1
        t = threading.Thread(target=srv.serve_forever, name=f"http-{key}", daemon=True)
        t.start()
        servers.append(srv)
        logger.info("%s UI on http://%s:%d", key.capitalize(), host, port)
    if not problem:
        remember_good(cfg)                    # these settings started the dashboard

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    # systemd watchdog (WatchdogSec in the unit): "alive" only while the loops that keep the
    # vehicle informed and AIO NAV running still turn; a hung dashboard is then restarted.
    ping = sdnotify.watchdog_interval_s()
    sdnotify.notify("READY=1")
    while not stop.wait(ping or 3600):
        if ping and ctx.alive():
            sdnotify.notify("WATCHDOG=1")

    logger.info("shutting down")
    for srv in servers:
        srv.shutdown()
    ctx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
