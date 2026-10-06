"""Health evaluation (runs at ~5 Hz, independent of NAV packet rate).

Product state is READY / INITIALIZING / FAULT and depends only on the AIO NAV
pipeline (process + fresh packets + required alignment flags). GNSS, camera,
LiDAR and network only produce advisories (spec §11–14).
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from .indicator import DebouncedStatus, FAULT, HEALTHY, UNKNOWN, WARNING

logger = logging.getLogger(__name__)

READY = "READY"
INITIALIZING = "INITIALIZING"
PRODUCT_FAULT = "FAULT"
PRODUCT_UNKNOWN = "UNKNOWN"
_PRODUCT_SEVERITY = {PRODUCT_UNKNOWN: 0, READY: 1, INITIALIZING: 2, PRODUCT_FAULT: 3}

SERVICE_DOWN_STATES = ("stopped", "failed", "not_installed")

ALIGN_LAMPS = ("alignment", "heading_valid", "fine_alignment")

FLAG_WAIT_TEXT = {
    "alignment": "Waiting for alignment",
    "heading_valid": "Waiting for valid heading",
    "fine_alignment": "Waiting for fine alignment",
}


def raw_product_state(nav: Dict, service_state: Optional[str], ncfg: Dict,
                      in_grace: bool) -> Tuple[str, List[str]]:
    """Undebounced product state and human-readable reasons."""
    age = nav.get("age_s")
    if service_state in SERVICE_DOWN_STATES:
        return PRODUCT_FAULT, ["AIO NAV process is not running"]
    if not nav.get("listening") and nav.get("bind_error"):
        return PRODUCT_FAULT, [f"NAV listener error: {nav['bind_error']}"]
    if age is None:
        if in_grace:
            return PRODUCT_UNKNOWN, ["Waiting for NAV packets"]
        return PRODUCT_FAULT, ["No NAV packets received"]
    if age > ncfg["stale_fault_s"]:
        return PRODUCT_FAULT, [f"NAV output stopped (last packet {age:.1f} s ago)"]
    latest = nav.get("latest") or {}
    missing = [f for f in ncfg["ready_requires"] if not latest.get(f)]
    if missing:
        return INITIALIZING, [FLAG_WAIT_TEXT.get(f, f"Waiting for {f}") for f in missing]
    return READY, []


def raw_udp_level(nav: Dict, ncfg: Dict, expected: Optional[float],
                  in_grace: bool) -> Tuple[str, str, str]:
    """(level, label, detail) for the UDP NAV output stream."""
    age = nav.get("age_s")
    if age is None:
        return (UNKNOWN, "Waiting", "No packets yet") if in_grace else \
               (FAULT, "Lost", "No NAV packets received")
    if age > ncfg["stale_fault_s"]:
        return FAULT, "Lost", f"Last packet {age:.1f} s ago"
    if age > ncfg["stale_warn_s"]:
        return WARNING, "Stale", f"Last packet {age * 1000:.0f} ms ago"
    rate = nav.get("rate_hz") or 0.0
    if expected and rate < ncfg["low_rate_ratio"] * expected:
        return WARNING, "Low rate", f"{rate:.0f} Hz (expected {expected:.0f} Hz)"
    return HEALTHY, "Streaming", ""


class HealthEngine:
    def __init__(self, cfg, store, nav, events, interval_s: float = 0.2,
                 clock=time.monotonic):
        self.cfg = cfg
        self.store = store
        self.nav = nav
        self.events = events
        self.interval_s = interval_s
        self._clock = clock
        self.started = clock()
        self.product = DebouncedStatus(0.5, 2.0, initial=PRODUCT_UNKNOWN,
                                       severity=lambda s: _PRODUCT_SEVERITY.get(s, 0), clock=clock)
        self.udp = DebouncedStatus(1.0, 2.0, clock=clock)
        self.gnss = DebouncedStatus(1.0, 2.0, clock=clock)
        # One lamp per filter-alignment flag; the product is Ready only when all of
        # nav.ready_requires are set.
        self.align_lamps = {k: DebouncedStatus(0.5, 0.5, clock=clock) for k in ALIGN_LAMPS}
        ping_s = float(cfg["network"]["ping_interval_s"])
        self.devices = {k: DebouncedStatus(ping_s * 1.2, ping_s * 0.8, clock=clock)
                        for k in cfg["devices"]}
        self.link = DebouncedStatus(2.0, 2.0, clock=clock)
        self._product_reasons: List[str] = []
        self._udp_label = ("Waiting", "")
        self._prev: Dict[str, str] = {}
        self._last_nav_pids: Optional[Tuple[int, ...]] = None
        self._fresh_since: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ----------------------------------------------------------------- thread
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("health evaluation failed")
            self._stop.wait(self.interval_s)

    # ------------------------------------------------------------------ eval
    def _track(self, key: str, value: str, level: str, category: str, title: str,
               detail: str = "") -> None:
        prev = self._prev.get(key)
        self._prev[key] = value
        # First observations and the startup transition out of "unknown" are not events.
        if prev is None or (prev in (UNKNOWN, "unknown") and key != "product"):
            return
        if prev != value:
            self.events.add(level, category, title, detail)

    def _check_nav_restart(self, services: Dict) -> None:
        svc = services.get(self.cfg["nav"]["service"]) or {}
        pids = tuple(sorted(svc.get("pids") or []))
        if pids and self._last_nav_pids and pids != self._last_nav_pids:
            self.nav.new_session("aio_nav_node restarted")
        if pids:
            self._last_nav_pids = pids

    def step(self) -> None:
        now = self._clock()
        ncfg = self.cfg["nav"]
        in_grace = now - self.started < ncfg["startup_grace_s"]
        services = self.store.get("services", {}) or {}
        self._check_nav_restart(services)

        nav = self.nav.status()
        nav_svc = services.get(ncfg["service"]) or {}
        service_state = nav_svc.get("state")
        expected = self.cfg.expected_nav_rate
        fresh = nav.get("age_s") is not None and nav["age_s"] <= ncfg["stale_fault_s"]
        latest = nav.get("latest") or {}
        flag_age = nav.get("flag_age_s", {})
        if not fresh:
            self._fresh_since = None
        elif self._fresh_since is None:
            self._fresh_since = now

        # Product state (debounced); reasons follow the displayed state.
        raw_state, reasons = raw_product_state(nav, service_state, ncfg, in_grace)
        shown = self.product.update(raw_state, now)
        if shown == raw_state:
            self._product_reasons = reasons
        elif not self._product_reasons and reasons:
            self._product_reasons = reasons
        self._track("product", shown, {READY: "success", INITIALIZING: "info"}.get(shown, "fault"),
                    "health", f"AIO NAV state {shown}", "; ".join(self._product_reasons))

        # UDP output stream.
        u_level, u_label, u_detail = raw_udp_level(nav, ncfg, expected, in_grace)
        u_shown = self.udp.update(u_level, now)
        if u_shown == u_level:
            self._udp_label = (u_label, u_detail)
        self._track("udp", u_shown, "warning" if u_shown != HEALTHY else "success", "dataflow",
                    f"UDP NAV output {self._udp_label[0]}", self._udp_label[1])

        # Alignment indicator.
        align_lamps = {}
        for flag in ALIGN_LAMPS:
            if not fresh:
                raw, label = UNKNOWN, "No data"
            elif latest.get(flag):
                raw, label = HEALTHY, "Done"
            else:
                raw, label = WARNING, "In progress"
            align_lamps[flag] = {"level": self.align_lamps[flag].update(raw, now), "label": label}

        # GNSS (advisory only).
        gnss_age = flag_age.get("gnss")
        gnss_recent = gnss_age is not None and gnss_age <= ncfg["gnss_timeout_s"]
        if not fresh:
            g_raw = UNKNOWN
        elif gnss_recent:
            g_raw = HEALTHY
        elif now - self._fresh_since < ncfg["gnss_timeout_s"]:
            g_raw = UNKNOWN  # stream just (re)started: too early to call GNSS unavailable
        else:
            g_raw = WARNING
        g_level = self.gnss.update(g_raw, now)
        self._track("gnss", g_level, "warning" if g_level == WARNING else "info", "gnss",
                    {WARNING: "GNSS unavailable", HEALTHY: "GNSS available"}.get(g_level, "GNSS unknown"),
                    "Inertial navigation remains active." if g_level == WARNING else "")

        aiding = {k: (flag_age.get(k) is not None and flag_age[k] <= ncfg["flag_active_s"] and fresh)
                  for k in ("zupt", "zihr", "nhc", "vupt", "gnss")}

        network = self.store.get("network", {}) or {}
        indicators = {
            **align_lamps,
            "gnss": {"level": g_level,
                     "label": {HEALTHY: "Available", WARNING: "Unavailable"}.get(g_level, "Unknown"),
                     "detail": "Inertial navigation remains active." if g_level == WARNING else ""},
        }
        advisories: List[Dict] = []
        if g_level == WARNING:
            advisories.append({"level": WARNING, "title": "GNSS signal unavailable",
                               "detail": "Inertial navigation remains active."})

        for key, dev_cfg in self.cfg["devices"].items():
            d = (network.get("devices") or {}).get(key) or {}
            reach = d.get("reachable")
            d_raw = UNKNOWN if reach is None else (HEALTHY if reach else WARNING)
            d_level = self.devices[key].update(d_raw, now)
            label = dev_cfg.get("label", key)
            indicators[key] = {"level": d_level, "label": {HEALTHY: "Connected",
                               WARNING: "Not connected"}.get(d_level, "Unknown")}
            if not dev_cfg.get("host"):
                indicators[key]["label"] = "Not set up"  # optional: no IP to check
            self._track(f"dev:{key}", d_level, "warning" if d_level == WARNING else "info",
                        "network", f"{label} " + {WARNING: "unreachable", HEALTHY: "reachable"}.get(d_level, "unknown"),
                        d.get("host", ""))
            if d_level == WARNING:
                advisories.append({"level": WARNING, "title": f"{label} not connected",
                                   "detail": f"{d.get('host', '')} does not respond"})

        link = network.get("sensor_link_up")
        l_level = self.link.update(UNKNOWN if link is None else (HEALTHY if link else WARNING), now)
        indicators["network"] = {"level": l_level, "label": {HEALTHY: "Link up",
                                 WARNING: "Link down"}.get(l_level, "Unknown")}
        if l_level == WARNING:
            advisories.append({"level": WARNING, "title": "Sensor LAN link down",
                               "detail": network.get("sensor_interface", "")})
        if u_shown == WARNING:
            advisories.append({"level": WARNING, "title": f"UDP output {self._udp_label[0].lower()}",
                               "detail": self._udp_label[1]})

        system = self.store.get("system", {}) or {}
        storage = self._storage(system)
        if storage and storage["percent"] >= self.cfg["system"]["disk_warn_percent"]:
            advisories.append({"level": WARNING, "title": "Storage almost full",
                               "detail": f"{storage['free'] / 1e9:.1f} GB free"})

        nav_out = {k: v for k, v in nav.items() if k not in ("latest", "flag_age_s")}
        nav_out["expected_rate_hz"] = expected
        nav_out["fresh"] = fresh
        self.store.set("product", {
            "ts": time.time(),
            "health": {"state": shown, "since_s": now - self.product.since,
                       "reasons": self._product_reasons},
            "nav": nav_out,
            "solution": latest if latest else None,
            "udp": {"level": u_shown, "label": self._udp_label[0], "detail": self._udp_label[1],
                    "rate_hz": nav.get("rate_hz"), "expected_rate_hz": expected,
                    "age_s": nav.get("age_s"), "destinations": self._destinations(network)},
            "indicators": indicators,
            "control": self._nav_control(nav_svc),
            "aiding": aiding,
            "advisories": advisories,
            "storage": storage,
            "uptime_s": system.get("uptime_s"),
            "session_uptime_s": time.time() - nav.get("session_started", time.time()),
        })
        self.store.set("maint", self._maintenance(now, services, network, system,
                                                  shown, u_shown, nav, expected))

    def _nav_control(self, nav_svc: Dict) -> Dict:
        """What the Overview page may offer for starting/stopping AIO NAV."""
        svc_cfg = self.cfg["services"].get(self.cfg["nav"]["service"]) or {}
        enabled = bool(self.cfg["nav"]["allow_control"] and svc_cfg.get("launch"))
        state = nav_svc.get("state") or "unknown"
        installed = bool(svc_cfg.get("launch") and self.cfg.launcher(svc_cfg["launch"]))
        info = self.cfg.ros_mode_info()
        return {"enabled": enabled, "state": state, "installed": installed,
                "label": svc_cfg.get("label", "AIO NAV"),
                "ros": {"mode": info["mode"], "label": info["label"], "domain_id": info["domain_id"]}}

    def _destinations(self, network: Dict) -> List[Dict]:
        routes = network.get("nav_routes") or {}
        out = []
        for d in self.cfg.nav_destinations():
            entry = dict(d)
            entry["route"] = routes.get(f'{d["host"]}:{d["port"]}')
            out.append(entry)
        return out

    def _storage(self, system: Dict) -> Optional[Dict]:
        disks = system.get("disks") or []
        if not disks:
            return None
        roots = [d for d in disks if d["path"] not in self.cfg["system"]["disk_paths"]]
        return (roots or disks)[0]

    # ----------------------------------------------------------- maintenance
    def _maintenance(self, now, services, network, system, product_state, udp_level,
                     nav, expected) -> Dict:
        issues: List[Dict] = []
        scfg = self.cfg["system"]

        def issue(layer, level, title, detail="", link=""):
            issues.append({"layer": layer, "level": level, "title": title,
                           "detail": detail, "link": link})

        # System layer
        if system:
            if (system.get("cpu_percent") or 0) >= 95:
                issue("system", WARNING, "CPU usage high", f"{system['cpu_percent']:.0f}%", "system")
            if (system.get("mem_percent") or 0) >= 90:
                issue("system", WARNING, "Memory usage high", f"{system['mem_percent']:.0f}%", "system")
            for d in system.get("disks", []):
                if d["percent"] >= scfg["disk_warn_percent"]:
                    issue("system", WARNING, "Disk almost full", f"{d['path']} {d['percent']:.0f}%", "system")
            if (system.get("max_temp_c") or 0) >= scfg["temp_warn_c"]:
                issue("system", WARNING, "Temperature high", f"{system['max_temp_c']:.0f} °C", "system")

        # Services layer
        nav_key = self.cfg["nav"]["service"]
        for key, svc in services.items():
            state = svc.get("state")
            self._track(f"svc:{key}", state or "unknown",
                        "success" if state == "running" else "warning", "service",
                        f"{svc.get('label', key)} {state}")
            # A sensor driver unit that does not exist is "not set up", not a problem.
            optional = (self.cfg["services"].get(key) or {}).get("optional")
            if state in SERVICE_DOWN_STATES and not optional and (key == nav_key or state != "not_installed"):
                level = FAULT if key == nav_key else WARNING
                issue("services", level, f"{svc.get('label', key)} {state.replace('_', ' ')}",
                      svc.get("unit", ""), "system")

        # Data flow layer
        dataflow = [{"name": "NAV UDP", "rate_hz": nav.get("rate_hz"), "expected_hz": expected,
                     "age_s": nav.get("age_s"), "level": udp_level, "link": ""}]
        if udp_level == FAULT:
            issue("dataflow", FAULT, "NAV UDP output lost", self._udp_label[1], "")
        ros = self.store.get("ros", {}) or {}
        if ros.get("available"):
            for t in ros.get("watched", []):
                if t["state"] == "not_found":
                    dataflow.append({"name": t["label"], "topic": t["name"], "rate_hz": None,
                                     "level": UNKNOWN, "state": t["state"], "detail": "not found",
                                     "link": "ros"})
                    continue
                level = {"healthy": HEALTHY, "low_rate": WARNING}.get(t["state"], FAULT)
                group = t.get("group", "")
                link = group if group in ("camera", "lidar") else "ros"
                dataflow.append({"name": t["label"], "topic": t["name"], "rate_hz": t["rate_hz"],
                                 "expected_hz": t.get("expected_hz"), "age_s": t["age_s"],
                                 "level": level, "state": t["state"], "link": link})
                self._track(f"topic:{t['name']}", t["state"],
                            "info" if t["state"] == "healthy" else "warning", "ros",
                            f"Topic {t['name']} {t['state'].replace('_', ' ')}")
                if level != HEALTHY:
                    issue("dataflow", WARNING, f"{t['name']} {t['state'].replace('_', ' ')}",
                          f"{t['rate_hz']:.1f} Hz", link)
            for n in ros.get("nodes", []):
                if not n["present"]:
                    issue("dataflow", WARNING, f"ROS node {n['name']} not found", "", "ros")
        else:
            dataflow.append({"name": "ROS 2", "level": UNKNOWN, "rate_hz": None,
                             "detail": ros.get("error") or "starting", "link": "ros"})

        # Network layer
        if network.get("sensor_link_up") is False:
            issue("network", WARNING, "Sensor LAN link down", network.get("sensor_interface", ""),
                  "network")
        for key, d in (network.get("devices") or {}).items():
            if d.get("reachable") is False:
                label = self.cfg["devices"][key].get("label", key)
                issue("network", WARNING, f"{label} unreachable", d.get("host", ""), key)
        for dest, r in (network.get("nav_routes") or {}).items():
            if r and not r.get("ok"):
                issue("network", WARNING, f"No route to NAV destination {dest}", r.get("error", ""),
                      "network")

        def layer_level(name):
            levels = [i["level"] for i in issues if i["layer"] == name]
            return FAULT if FAULT in levels else WARNING if levels else HEALTHY

        return {
            "ts": time.time(),
            "product_state": product_state,
            "layers": {name: layer_level(name) for name in ("system", "services", "dataflow", "network")},
            "issues": sorted(issues, key=lambda i: 0 if i["level"] == FAULT else 1),
            "dataflow": dataflow,
        }
