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
TOPIC_NOT_FOUND = "not_found"  # auto topic with no match: sensor not fitted, not an error


# Names used for "auto" topics in fake mode (match dev/fake_state.json).
FAKE_TOPIC_BY_TYPE = {
    "sensor_msgs/msg/PointCloud2": "/ouster/points",
    "sensor_msgs/msg/Imu": "/ouster/imu",
    "sensor_msgs/msg/CameraInfo": "/camera/camera_info",
    "sensor_msgs/msg/CompressedImage": "/camera/image_raw/compressed",
    "sensor_msgs/msg/Image": "/camera/image_raw",
    "nav_msgs/msg/Odometry": "/nav/odometry",
}


def pick_topic(topic_types: Dict[str, List[str]], types, hint: str = "", strict: bool = False,
               avoid=("depth", "/nav/", "/odometry/raw", "/clock")) -> Optional[str]:
    """Choose the topic to watch among those publishing one of ``types``.

    Preference: name contains ``hint``; name avoids depth images and the nav / input
    topics; earlier entry in ``types``; shorter name.
    """
    types = [types] if isinstance(types, str) else list(types)
    cands = [n for n, ts in topic_types.items() if any(t in ts for t in types)]
    if strict and hint:
        cands = [n for n in cands if hint in n]
    if not cands:
        return None

    def key(n):
        type_rank = min(types.index(t) for t in topic_types[n] if t in types)
        return (0 if hint and hint in n else 1,
                any(a in n.lower() for a in avoid), type_rank, len(n), n)
    return min(cands, key=key)


def is_auto(name: Optional[str]) -> bool:
    return not name or name == "auto"


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


def in_sampling_window(now: float, t0: float, period_s: float, window_s: float) -> bool:
    """Is `now` inside the listening part of the sampling cycle? Each period starts with a window."""
    return (now - t0) % period_s < window_s


class _TopicStats:
    def __init__(self, window_s: float):
        self.window_s = window_s
        self.times: deque = deque()
        self.last: Optional[float] = None
        self.lock = threading.Lock()
        # Sampled topics only (see Ros2Collector): result of the last listening window.
        self.sampling = False
        self.frozen: Optional[tuple] = None   # (rate, age at the end of the window, ended at)

    def tick(self) -> None:
        now = time.monotonic()
        with self.lock:
            self.last = now
            self.times.append(now)
            while self.times and self.times[0] < now - self.window_s:
                self.times.popleft()

    def _live(self, now: float):
        """Rate and age as of `now`. The caller holds the lock."""
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

    def read(self):
        now = time.monotonic()
        with self.lock:
            return self._live(now)

    def start_window(self) -> None:
        with self.lock:
            self.times.clear()
            self.last = None
            self.sampling = True

    def end_window(self) -> None:
        now = time.monotonic()
        with self.lock:
            rate, age = self._live(now) if self.last is not None else (0.0, None)
            self.frozen = (rate, age, now)      # nothing heard in the window = a stale topic
            self.sampling = False

    def read_sampled(self):
        """(rate, age, seconds since measured). Between windows this is the last window's result;
        inside a window it switches to live values once there are enough messages to trust."""
        now = time.monotonic()
        with self.lock:
            if self.sampling and len(self.times) >= 2:
                rate, age = self._live(now)
                return rate, age, 0.0
            if self.frozen is not None:
                rate, age, at = self.frozen
                return rate, age, now - at
            rate, age = self._live(now)
            return rate, age, 0.0


class Ros2Collector:
    name = "ros2"
    section = "ros"

    def __init__(self, store, cfg, fake=None, preview=None, watchers=(), guard=None):
        self.store = store
        self.preview = preview  # PreviewTap: on-demand camera/LiDAR subscriptions
        self.guard = guard  # RosIsolationGuard: restarts the dashboard if its ROS connection is stuck
        self.watchers = list(watchers)  # e.g. DsoWatchdog.attach(node, qos)
        self.rcfg = cfg["ros"]
        self.fake = fake
        self.watched: List[Dict] = list(self.rcfg.get("topics", []))
        self.nodes_cfg: List[str] = list(self.rcfg.get("nodes", []))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stats: Dict[str, _TopicStats] = {}
        self._subs: Dict[str, object] = {}
        self._sampled: Dict[str, Dict] = {}     # name -> {"type", "cls", "sub"}: high-rate topics
        self._t0 = time.monotonic()
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
            for spec in self.watched:
                t = dict(spec)
                if is_auto(t.get("name")):
                    t["name"] = FAKE_TOPIC_BY_TYPE.get(t.get("type", ""), "/auto")
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
            q = self.fake.get().get("gnss", "fixed")   # fixed | float | spp | none
            self.store.set("gnss", {"available": True, "quality": q,
                                    "label": {"fixed": "RTK fix", "float": "RTK float",
                                              "spp": "SPP", "none": "No signal"}.get(q, "No signal"),
                                    "status": {"fixed": 2, "float": 1, "spp": 0}.get(q, -1),
                                    "sigma_h_m": {"fixed": 0.03, "float": 0.09, "spp": 10.0}.get(q),
                                    "age_s": 0.5, "topic": "(fake)", "messages": 0})
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
        if (self.rcfg.get("sampling") or {}).get("enabled"):
            node.create_timer(0.25, self._sample_tick)
        for w in self.watchers:
            try:
                w.attach(node, self._qos)
            except Exception:
                logger.exception("could not attach %s", type(w).__name__)
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

    # ------------------------------------------------- sampling of high-rate topics
    def _sampling_on(self, spec: Dict) -> bool:
        s = self.rcfg.get("sampling") or {}
        hz = spec.get("expected_hz")
        return bool(s.get("enabled") and hz and float(hz) >= float(s.get("above_hz", 30)))

    def _sample_start(self, name: str, info: Dict) -> None:
        if info.get("cls") is None:
            try:
                from rosidl_runtime_py.utilities import get_message
                info["cls"] = get_message(info["type"])
            except (ImportError, AttributeError, ModuleNotFoundError, ValueError) as e:
                logger.warning("cannot load message type %s for %s: %s", info["type"], name, e)
                info["cls"] = False
                return
        if info["cls"] is False:
            return
        stats = self._stats.setdefault(name, _TopicStats(float(self.rcfg["rate_window_s"])))
        stats.start_window()
        info["sub"] = self._node.create_subscription(
            info["cls"], name, lambda _msg, s=stats: s.tick(), self._qos, raw=True)

    def _sample_stop(self, name: str) -> None:
        info = self._sampled.get(name)
        if not info or info.get("sub") is None:
            return
        try:
            self._node.destroy_subscription(info["sub"])
        except Exception:
            logger.exception("could not end the sampling window of %s", name)
        info["sub"] = None
        if name in self._stats:
            self._stats[name].end_window()

    def _sample_tick(self) -> None:
        """Open or close the listening window of every sampled topic (timer, executor thread)."""
        s = self.rcfg["sampling"]
        want = in_sampling_window(time.monotonic(), self._t0, float(s["period_s"]), float(s["window_s"]))
        for name, info in list(self._sampled.items()):
            active = info.get("sub") is not None
            if want and not active:
                self._sample_start(name, info)
            elif not want and active:
                self._sample_stop(name)

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
        for spec in self.watched:
            t = dict(spec)
            if is_auto(t.get("name")):
                found = pick_topic(topic_types, t.get("type", ""), t.get("hint", ""),
                                   bool(t.get("strict")))
                short = t.get("type", "topic").rsplit("/", 1)[-1]
                t["name"] = found or f"(no {short} topic)"
                t["auto"] = True
            name = t["name"]
            types = topic_types.get(name)
            pubs = node.count_publishers(name) if types else 0
            subs = node.count_subscribers(name) if types else 0
            sampled = self._sampling_on(t)
            if types and pubs:
                if sampled:
                    self._sampled.setdefault(name, {"type": t.get("type") or types[0]})
                else:
                    self._ensure_subscription(name, t.get("type") or types[0])
            elif sampled:
                self._sample_stop(name)          # the topic is gone: stop listening
            own = self._subs.get(name) is not None or (self._sampled.get(name) or {}).get("sub") is not None
            if own:
                subs = max(0, subs - 1)  # do not count the dashboard's own subscription
            ago = None
            if sampled and name in self._stats:
                rate, age, ago = self._stats[name].read_sampled()
            else:
                rate, age = self._stats[name].read() if name in self._stats else (0.0, None)
            entry = self._topic_entry(t, bool(types), pubs, subs, rate, age)
            entry["sampled"] = sampled
            entry["sample_age_s"] = ago
            watched_out.append(entry)

        graph = []
        own = "/aio_dashboard_monitor"
        if self.guard is not None:
            foreign = [n for n in node_names if n != own and not n.startswith("/_ros2cli")]
            self.guard.update(len(foreign))
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
            "state": (TOPIC_NOT_FOUND if t.get("auto") and not present
                      else classify_topic(present, pubs, age, rate, expected)),
            "auto": bool(t.get("auto")),
        }
