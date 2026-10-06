"""AIO System Dashboard entry point.

One process, shared state, two HTTP servers:
  Product UI      :8080  (Overview / Navigation / Data)
  Maintenance UI  :8081  (System / ROS 2 / Camera / LiDAR / Network / Diagnostics)
"""

import argparse
import logging
import signal
import socket
import threading

from werkzeug.serving import make_server

from .actions.registry import ActionRegistry
from .collectors.fake import FakeState
from .collectors.nav_udp import NavUdpCollector
from .collectors.network import NetworkCollector
from .collectors.ros2 import Ros2Collector
from .collectors.services import ServicesCollector
from .collectors.system import SystemCollector
from .config import load_config, resolve_path
from .data_access.files import DataRoots
from .state.health import HealthEngine
from .state.store import EventLog, StateStore
from .web.maintenance import create_maintenance_app
from .web.product import create_product_app

logger = logging.getLogger("aio_dashboard")


class DashboardContext:
    """Everything the web apps need; collectors write, web apps read."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.hostname = socket.gethostname()
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
        self.ros = Ros2Collector(self.store, cfg, self.fake)
        self.health = HealthEngine(cfg, self.store, self.nav, self.events)
        self.actions = ActionRegistry(cfg, self.store, self.events, self.network.check_device)
        self.data = DataRoots(cfg["data"]["roots"])
        self._workers = [self.nav, self.system, self.services, self.network, self.ros, self.health]

    def start(self):
        for w in self._workers:
            w.start()

    def stop(self):
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.join()


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

    cfg = load_config(args.config, force_fake=args.fake)
    ctx = DashboardContext(cfg)
    ctx.events.add("info", "system", "Dashboard started", "fake mode" if cfg.fake else "")
    ctx.start()

    servers = []
    for key, factory in (("product", create_product_app), ("maintenance", create_maintenance_app)):
        host, port = cfg[key]["host"], int(cfg[key]["port"])
        try:
            srv = make_server(host, port, factory(ctx), threaded=True)
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

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()

    logger.info("shutting down")
    for srv in servers:
        srv.shutdown()
    ctx.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
