"""ROS 2 Level-2 monitoring: node presence, topic existence, pub/sub counts, rate, freshness.

Watched topics are subscribed with ``raw=True`` so messages are never
deserialized (no PointCloud2/image decoding). Prefer lightweight topics such as
``camera_info`` in the config to avoid pulling large payloads.

rclpy is optional: on a dev machine without ROS the collector reports
``available: false`` (or uses fake data when fake mode is enabled).
"""

import logging
import random
import threading
import time
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TOPIC_HEALTHY = "healthy"
TOPIC_LOW_RATE = "low_rate"
TOPIC_STALE = "stale"
TOPIC_MISSING = "missing"


def stale_after_s(expected_hz: Optional[float]) -> float:
    if expected_hz and expected_hz > 0:
        return max(1.0, 5.0 / expected_hz)
    return 3.0


def classify_topic(present: bool, publishers: int, age_s: Optional[float],
                   rate_hz: float, expected_hz: Optional[float]) -> str:
    if not present or publishers == 0:
        return TOPIC_MISSING
    if age_s is None or age_s > stale_after_s(expected_hz):
        return TOPIC_STALE
    if expected_hz and rate_hz < 0.5 * expected_hz:
        return TOPIC_LOW_RATE
    return TOPIC_HEALTHY


class _TopicStats:
    def __init__(self, window_s: float):
        self.window_s = window_s
        self.times: deque = deque()
        self.last: Optional[float] = None
        self.lock = threading.Lock()

    def tick(self) -> None:
        now = time.monotonic()
        with self.lock:
            self.last = now
            self.times.append(now)
            while self.times and self.times[0] < now - self.window_s:
                self.times.popleft()

    def read(self):
        now = time.monotonic()
        with self.lock:
            while self.times and self.times[0] < now - self.window_s:
                self.times.popleft()
            n = len(self.times)
            if n >= 2:
                span = max(self.times[-1] - self.times[0], 1e-6)
                rate = (n - 1) / span if now - self.times[-1] < self.window_s else 0.0
            else:
                rate = 0.0
            age = now - self.last if self.last is not None else None
        return rate, age


class Ros2Collector:
    name = "ros2"
    section = "ros"

    def __init__(self, store, cfg, fake=None, preview=None):
        self.store = store
        self.preview = preview  # PreviewTap: on-demand camera/LiDAR subscriptions
        self.rcfg = cfg["ros"]
        self.fake = fake
        self.watched: List[Dict] = list(self.rcfg.get("topics", []))
        self.nodes_cfg: List[str] = list(self.rcfg.get("nodes", []))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stats: Dict[str, _TopicStats] = {}
        self._subs: Dict[str, object] = {}
        self._first_seen: Dict[str, float] = {}

    def start(self) -> None:
        target = self._run_fake if self.fake is not None else self._run_rclpy
        self._thread = threading.Thread(target=target, name=self.name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 3.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------------ fake
    def _run_fake(self) -> None:
        while not self._stop.is_set():
            topics = []
            for t in self.watched:
                f = self.fake.topic(t["name"])
                expected = t.get("expected_hz")
                if f["state"] == TOPIC_MISSING:
                    rate, age, pubs = 0.0, None, 0
                elif f["state"] == TOPIC_STALE:
                    rate, age, pubs = 0.0, 12.0, 1
                else:
                    base = f["hz"] if f["hz"] is not None else (expected or 10.0)
                    if f["state"] == TOPIC_LOW_RATE and f["hz"] is None:
                        base *= 0.3
                    rate = base * random.uniform(0.97, 1.03)
                    age, pubs = 1.0 / max(rate, 0.1), 1
                topics.append(self._topic_entry(t, pubs > 0, pubs, 1 if pubs else 0, rate, age))
            nodes = [{"name": n, "present": self.fake.node_present(n)} for n in self.nodes_cfg]
            self.store.set(self.section, {
                "available": True, "fake": True, "error": None, "nodes": nodes,
                "watched": topics,
                "graph": [{"name": t["name"], "types": [t.get("type", "")],
                           "publishers": e["publishers"], "subscribers": e["subscribers"]}
                          for t, e in zip(self.watched, topics)],
            })
            self._stop.wait(float(self.rcfg["graph_interval_s"]))

    # ---------------------------------------------------------------- rclpy
    def _run_rclpy(self) -> None:
        if not self.rcfg.get("enabled", True):
            self.store.set(self.section, {"available": False, "error": "disabled in config"})
            return
        try:
            import rclpy
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import qos_profile_sensor_data
        except ImportError as e:
            self.store.set(self.section, {"available": False,
                                          "error": f"rclpy not available ({e}); source ROS 2"})
            logger.warning("ROS 2 monitoring disabled: %s", e)
            return

        self._qos = qos_profile_sensor_data
        ctx = rclpy.Context()
        try:
            rclpy.init(context=ctx)
            node = rclpy.create_node("aio_dashboard_monitor", context=ctx)
        except Exception as e:  # rclpy raises a variety of RCL errors here
            self.store.set(self.section, {"available": False, "error": f"rclpy init failed: {e}"})
            logger.exception("rclpy init failed")
            return
        self._node = node
        executor = SingleThreadedExecutor(context=ctx)
        executor.add_node(node)
        node.create_timer(float(self.rcfg["graph_interval_s"]), self._refresh_graph)
        if self.preview is not None and self.preview.sources:
            node.create_timer(0.5, self._sync_preview)
        self._refresh_graph()
        logger.info("ROS 2 monitor started (watching %d topics)", len(self.watched))
        try:
            while not self._stop.is_set() and ctx.ok():
                executor.spin_once(timeout_sec=0.2)
        finally:
            executor.shutdown()
            node.destroy_node()
            try:
                rclpy.shutdown(context=ctx)
            except Exception:
                pass

    def _sync_preview(self) -> None:
        try:
            self.preview.sync(self._node, dict(self._node.get_topic_names_and_types()))
        except Exception:
            logger.exception("preview subscription sync failed")

    def _ensure_subscription(self, name: str, type_name: str) -> None:
        if name in self._subs:
            return
        try:
            from rosidl_runtime_py.utilities import get_message
            msg_cls = get_message(type_name)
        except (ImportError, AttributeError, ModuleNotFoundError, ValueError) as e:
            logger.warning("cannot load message type %s for %s: %s", type_name, name, e)
            self._subs[name] = None
            return
        stats = self._stats.setdefault(name, _TopicStats(float(self.rcfg["rate_window_s"])))
        self._subs[name] = self._node.create_subscription(
            msg_cls, name, lambda _msg, s=stats: s.tick(), self._qos, raw=True)

    def _refresh_graph(self) -> None:
        node = self._node
        try:
            topic_types = dict(node.get_topic_names_and_types())
            node_names = {
                (ns.rstrip("/") + "/" + n) if ns != "/" else "/" + n
                for n, ns in node.get_node_names_and_namespaces()
            }
        except Exception as e:
            self.store.set(self.section, {"available": False, "error": f"graph query failed: {e}"})
            return

        watched_out = []
        for t in self.watched:
            name = t["name"]
            types = topic_types.get(name)
            pubs = node.count_publishers(name) if types else 0
            subs = node.count_subscribers(name) if types else 0
            if types and pubs:
                self._ensure_subscription(name, t.get("type") or types[0])
            if self._subs.get(name) is not None:
                subs = max(0, subs - 1)  # do not count the dashboard's own subscription
            rate, age = self._stats[name].read() if name in self._stats else (0.0, None)
            watched_out.append(self._topic_entry(t, bool(types), pubs, subs, rate, age))

        graph = []
        own = "/aio_dashboard_monitor"
        for name, types in sorted(topic_types.items())[:300]:
            subs = node.count_subscribers(name)
            if self._subs.get(name) is not None:
                subs = max(0, subs - 1)
            graph.append({"name": name, "types": types,
                          "publishers": node.count_publishers(name), "subscribers": subs})
        self.store.set(self.section, {
            "available": True, "fake": False, "error": None,
            "nodes": [{"name": n, "present": n in node_names} for n in self.nodes_cfg],
            "all_nodes": sorted(n for n in node_names if n != own),
            "watched": watched_out,
            "graph": graph,
        })

    @staticmethod
    def _topic_entry(t: Dict, present: bool, pubs: int, subs: int,
                     rate: float, age: Optional[float]) -> Dict:
        expected = t.get("expected_hz")
        return {
            "name": t["name"],
            "group": t.get("group", ""),
            "label": t.get("label", t["name"]),
            "expected_hz": expected,
            "present": present,
            "publishers": pubs,
            "subscribers": subs,
            "rate_hz": round(rate, 2),
            "age_s": age,
            "state": classify_topic(present, pubs, age, rate, expected),
        }
