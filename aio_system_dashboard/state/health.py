"""Health evaluation (runs at ~5 Hz, independent of NAV packet rate).

Product state depends only on the AIO NAV pipeline (process + fresh packets + required alignment
flags). GNSS, camera, LiDAR and network only produce advisories (spec §11–14).

  STOPPED       not running, and nobody asked it to run: normal, not a problem
  STARTING      asked to start / just started, no output yet (up to nav.startup_timeout_s)
  INITIALIZING  output flowing, alignment steps not all done (the current step is reported)
  READY         navigation solution available
  FAULT         only when AIO NAV has a real problem: it stopped although it should run, it runs
                but sends nothing after the start-up time, or its output stopped
"""

import datetime
import logging
import secrets
import threading
import time
from typing import Dict, List, Optional, Tuple

from ..actions import process_control
from . import readiness as rd
from .indicator import DebouncedStatus, FAULT, HEALTHY, UNKNOWN, WARNING

logger = logging.getLogger(__name__)

READY = "READY"
INITIALIZING = "INITIALIZING"
STARTING = "STARTING"
STOPPED = "STOPPED"
DEGRADED = "DEGRADED"              # was READY in this run, a required condition is now lost
PRODUCT_FAULT = "FAULT"
PRODUCT_UNKNOWN = "UNKNOWN"
_PRODUCT_SEVERITY = {PRODUCT_UNKNOWN: 0, STOPPED: 0, READY: 1, STARTING: 1, INITIALIZING: 2, PRODUCT_FAULT: 3}
IDLE = "idle"                      # lamp level: off on purpose (not unknown, not a problem)
START_REQUEST_GRACE_S = 10.0       # a start request with no process yet is "starting" this long

SERVICE_DOWN_STATES = ("stopped", "failed", "not_installed")

# GNSS receiver quality -> (lamp level, label)
GNSS_LEVEL = {"fixed": (HEALTHY, "RTK fix"), "float": (WARNING, "RTK float"),
              "spp": (FAULT, "SPP"), "none": (FAULT, "No signal"),
              # a position whose status the driver does not rate (it uses -1 for some fixes)
              "unrated": (WARNING, "Fix (type unknown)"), "unrated_poor": (FAULT, "Fix (type unknown)")}

ALIGN_LAMPS = ("alignment", "heading_valid", "fine_alignment")

FLAG_WAIT_TEXT = {
    "alignment": "Waiting for alignment",
    "heading_valid": "Waiting for valid heading",
    "fine_alignment": "Waiting for fine alignment",
}
# Alignment steps in order: (title, what the operator does). See OPERATION section 5.
STEPS = {
    "alignment": ("Alignment", "Keep the vehicle completely still (about 10 s)."),
    "heading_valid": ("Initial heading", "Drive straight at a steady speed under open sky."),
    "fine_alignment": ("Fine alignment", "Keep driving normally with GNSS available, with a few turns."),
}


def alignment_step(latest: Dict, required: List[str]) -> Optional[Dict]:
    """The first required alignment flag that is not set yet, with its position and instruction."""
    order = [f for f in ALIGN_LAMPS if f in required] + [f for f in required if f not in ALIGN_LAMPS]
    for i, flag in enumerate(order):
        if not latest.get(flag):
            title, action = STEPS.get(flag, (flag.replace("_", " ").capitalize(), FLAG_WAIT_TEXT.get(flag, "")))
            return {"index": i + 1, "total": len(order), "key": flag, "title": title, "action": action}
    return None


def raw_product_state(nav: Dict, run: Dict, ncfg: Dict) -> Tuple[str, List[str], Optional[Dict]]:
    """Undebounced product state, its two lines of text (what / what to do), and the alignment step.

    run: running (process seen), service_state, wanted (asked to run), wanted_age_s (since the
    last start request), run_age_s (since the process was first seen), in_grace (dashboard start).
    """
    age = nav.get("age_s")
    fresh = age is not None and age <= ncfg["stale_fault_s"]
    if not nav.get("listening") and nav.get("bind_error"):
        return PRODUCT_FAULT, ["Cannot receive navigation output", nav["bind_error"]], None
    state = run.get("service_state")
    if not run.get("running") and not fresh:
        if state == "not_installed":
            return PRODUCT_FAULT, ["AIO NAV is not installed on this device",
                                   "Its launcher was not found in the aio-nav-ros install folder."], None
        wanted_age = run.get("wanted_age_s")
        if run.get("wanted") and wanted_age is not None and wanted_age < START_REQUEST_GRACE_S:
            return STARTING, ["Starting AIO NAV", "This takes a few seconds."], None
        if state == "failed" or run.get("wanted"):
            return PRODUCT_FAULT, ["AIO NAV stopped unexpectedly",
                                   "Press Start to run it again. If it keeps stopping, check the Maintenance page."], None
        if state is None and run.get("in_grace"):
            return PRODUCT_UNKNOWN, ["Checking AIO NAV", ""], None
        return STOPPED, ["Not running", "Press Start to begin navigation."], None
    if age is None:
        run_age = run.get("run_age_s") or 0.0
        if run_age < ncfg["startup_timeout_s"]:
            return STARTING, ["Waiting for the first navigation output", "AIO NAV is starting up."], None
        return PRODUCT_FAULT, ["AIO NAV is running but sends no navigation output",
                               f"Nothing received in {run_age:.0f} s. Restart it, or check the Maintenance page."], None
    if not fresh:
        return PRODUCT_FAULT, ["Navigation output stopped",
                               f"Last output {age:.0f} s ago. Restart AIO NAV if it does not come back."], None
    step = alignment_step(nav.get("latest") or {}, ncfg["ready_requires"])
    if step:
        return INITIALIZING, [f"Step {step['index']} of {step['total']}: {step['title']}", step["action"]], step
    return READY, ["Navigation solution available", ""], None


def raw_udp_level(nav: Dict, ncfg: Dict, expected: Optional[float],
                  in_grace: bool, product_state: Optional[str] = None) -> Tuple[str, str, str]:
    """(level, label, detail) for the UDP NAV output stream. Off (idle) while AIO NAV is stopped
    or still starting: no output then is expected, not lost."""
    age = nav.get("age_s")
    if age is None:
        if product_state == STOPPED:
            return IDLE, "Off", "AIO NAV is not running"
        if product_state in (STARTING, PRODUCT_UNKNOWN) or in_grace:
            return UNKNOWN, "Waiting", "No output yet"
        return FAULT, "Lost", "No NAV packets received"
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
        self._gnss_label = "No data"
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
        self._run_since: Optional[float] = None       # when the current AIO NAV process was first seen
        self._step: Optional[Dict] = None
        self._fresh_since: Optional[float] = None
        # Readiness (state/readiness.py): one snapshot for the pages, /api/readiness and 0x05.
        self.session_id = secrets.randbits(32) or 1     # new at every dashboard start
        self._sequence = 0
        rcfg = cfg["readiness"]
        self.conditions = {n: rd.Condition(rcfg["gnss_stable_s"] if n == "gnss_quality" else rcfg["stable_s"],
                                           rcfg["loss_s"]) for n in rd.CONDITIONS}
        # READY reached by the current AIO NAV process. Per process, not per NAV session: when AIO
        # NAV dies the NAV state is cleared (a new session) at once, and the latch must survive that
        # so a crash while driving says "stop using navigation", not "starting".
        self._ready_run = False
        unknown = [n for n in (rcfg.get("require") or []) if n not in rd.CONDITIONS]
        if unknown:
            logger.error("readiness.require: unknown condition(s) %s; READY is blocked until fixed "
                         "(known: %s)", ", ".join(unknown), ", ".join(rd.CONDITIONS))
        self._duplicate = DebouncedStatus(3.0, 2.0, clock=clock)   # a stop/start overlap is not a fault
        self._rd_state: Optional[str] = None
        self._rd_since = clock()
        self.autostart_status = None                   # callable -> dict, set by the app
        self.status_publisher = None                   # 0x05 sender, set by the app
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
        if pids and pids != self._last_nav_pids:
            self._run_since = self._clock()          # a new process: its start-up time begins now
            self._ready_run = False                  # and it has to become READY again
        elif not pids:
            self._run_since = None
        if pids and self._last_nav_pids and pids != self._last_nav_pids:
            self.nav.new_session("aio_nav_node restarted")
        elif not pids and not self.nav.is_clear():
            # Not running: back to the default state (its next start begins from scratch). Packets
            # still arriving are shown, though: they mean AIO NAV runs where it is not detected.
            age = self.nav.status().get("age_s")
            if self._last_nav_pids or age is None or age > self.cfg["nav"]["stale_fault_s"]:
                self.nav.clear("AIO NAV stopped" if self._last_nav_pids else "AIO NAV not running")
        self._last_nav_pids = pids or None

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
        since = process_control.wanted_since(self.cfg, ncfg["service"])
        run = {"running": bool(nav_svc.get("pids")), "service_state": service_state,
               "wanted": process_control.wanted(self.cfg, ncfg["service"]),
               "wanted_age_s": (time.time() - since) if since else None,
               "run_age_s": (now - self._run_since) if self._run_since is not None else None,
               "in_grace": in_grace}
        raw_state, reasons, step = raw_product_state(nav, run, ncfg)
        shown = self.product.update(raw_state, now)
        if shown == raw_state:
            self._product_reasons, self._step = reasons, step
        elif not self._product_reasons and reasons:
            self._product_reasons, self._step = reasons, step
        self._track("product", shown, {READY: "success", PRODUCT_FAULT: "fault"}.get(shown, "info"),
                    "health", f"AIO NAV state {shown}", "; ".join(r for r in self._product_reasons if r))

        # UDP output stream.
        u_level, u_label, u_detail = raw_udp_level(nav, ncfg, expected, in_grace, raw_state)
        u_shown = self.udp.update(u_level, now)
        if u_shown == u_level:
            self._udp_label = (u_label, u_detail)
        self._track("udp", u_shown, {HEALTHY: "success", IDLE: "info", UNKNOWN: "info"}.get(u_shown, "warning"),
                    "dataflow", f"UDP NAV output {self._udp_label[0]}", self._udp_label[1])

        # Alignment lamps, one per step: done, the step in progress, later steps waiting.
        current = alignment_step(latest, ncfg["ready_requires"]) if fresh else None
        align_lamps = {}
        for flag in ALIGN_LAMPS:
            if not fresh:
                raw = UNKNOWN
            elif latest.get(flag):
                raw = HEALTHY
            elif current and current["key"] == flag:
                raw = WARNING
            else:
                raw = IDLE
            level = self.align_lamps[flag].update(raw, now)
            align_lamps[flag] = {"level": level, "label": {HEALTHY: "Done", WARNING: "In progress",
                                                           IDLE: "Waiting"}.get(level, "—")}

        # GNSS receiver quality (advisory only): green RTK fix, yellow RTK float, red SPP / no signal.
        gq = self.store.get("gnss", {}) or {}
        quality = gq.get("quality") if gq.get("available") else None
        g_raw, g_text = GNSS_LEVEL.get(quality, (UNKNOWN, "No data"))
        g_level = self.gnss.update(g_raw, now)
        if g_level == g_raw:
            self._gnss_label = g_text
        self._track("gnss", g_level, {HEALTHY: "info", WARNING: "warning", FAULT: "warning"}.get(g_level, "info"),
                    "gnss", f"GNSS {self._gnss_label}",
                    "Inertial navigation remains active." if g_level in (WARNING, FAULT) else "")

        aiding = {k: (flag_age.get(k) is not None and flag_age[k] <= ncfg["flag_active_s"] and fresh)
                  for k in ("zupt", "zihr", "nhc", "vupt", "gnss")}

        readiness = self._readiness(now, shown, nav, nav_svc, gq, fresh, latest, in_grace)
        health_view = self._health_view(shown, readiness)

        network = self.store.get("network", {}) or {}
        indicators = {
            **align_lamps,
            "gnss": {"level": g_level, "label": self._gnss_label if g_level != UNKNOWN else "No data",
                     "detail": self._gnss_detail(gq)},
        }
        advisories: List[Dict] = []
        if g_level == FAULT:
            if quality == "none":
                why = gq.get("reason") or "no usable position"
                detail = f"No GNSS position: {why}. Inertial navigation remains active."
            else:
                detail = "Position accuracy is poor. Inertial navigation remains active."
            advisories.append({"level": WARNING, "title": f"GNSS: {self._gnss_label}", "detail": detail})

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
            "health": health_view,
            "readiness": readiness,
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
            "session_uptime_s": (time.time() - nav["session_started"]) if nav.get("session_started") else None,
        })
        self.store.set("readiness", readiness)
        maint = self._maintenance(now, services, network, system, shown, u_shown, nav, expected)
        maint["product_state"] = health_view["state"]      # the same state the user pages show
        self.store.set("maint", maint)
        if self.status_publisher is not None:
            self.status_publisher.publish(readiness, now)

    # -------------------------------------------------------------- readiness
    def _imu_topic(self, ros: Dict) -> Optional[Dict]:
        name = self.cfg["readiness"].get("imu_topic") or "auto"
        watched = ros.get("watched") or []
        if name == "auto":
            return next((t for t in watched if t.get("group") == "nav"), None)
        return next((t for t in watched if t.get("name") == name), None)

    def _condition_inputs(self, gq: Dict) -> Dict[str, Tuple[Optional[bool], str]]:
        """(holds now, why not) for every optional READY condition, required or not (the health
        bits report them all)."""
        rcfg = self.cfg["readiness"]
        out: Dict[str, Tuple[Optional[bool], str]] = {}

        ros = self.store.get("ros", {}) or {}
        t = self._imu_topic(ros) if ros.get("available") else None
        if t is None:
            out["imu"] = (False, "No IMU topic seen" if ros.get("available") else "ROS 2 monitor not available")
        else:
            # The topic's state, not its raw age: with ros.sampling a 100 Hz topic is only listened
            # to in windows, so its age grows between them while its state stays "healthy".
            out["imu"] = (t.get("state") == "healthy", f"IMU {t['name']}: {t.get('state', 'unknown').replace('_', ' ')}")

        quality = gq.get("quality") if gq.get("available") else None
        out["gnss"] = (quality not in (None, "none"), "No GNSS position" if quality == "none" else "No GNSS data")
        want = rcfg.get("gnss_min_quality") or "float"
        need = rd.MIN_QUALITY.get(str(want).lower(), 3)
        label = GNSS_LEVEL.get(quality, (None, "no data"))[1]
        out["gnss_quality"] = (rd.gnss_rank(quality) >= need,
                               f"GNSS {label}; needs {rd.GNSS_CODE_NAMES[need].replace('_', ' ')} or better")

        dso = (self.store.get("watchdog", {}) or {}).get("dso") or {}
        age = dso.get("last_msg_age_s")
        dso_ok = dso.get("state") == "ok" and age is not None and age <= rcfg["dso_stale_s"]
        out["dso"] = (dso_ok, f"DSO odometry {dso.get('state') or 'not monitored'}"
                              + (f", last message {age:.1f} s ago" if age is not None and not dso_ok else ""))

        network = self.store.get("network", {}) or {}
        externals = [d for d in self.cfg.nav_destinations() if d.get("kind") == "external"]
        routes = network.get("nav_routes") or {}
        if not externals:
            out["vehicle_route"] = (False, "No vehicle destination set (output_udp in aio_nav.yaml)")
        else:
            bad = [f'{d["host"]}:{d["port"]}' for d in externals
                   if not (routes.get(f'{d["host"]}:{d["port"]}') or {}).get("ok")]
            out["vehicle_route"] = (not bad, f"No route to the vehicle {', '.join(bad)}")

        storage = self._storage(self.store.get("system", {}) or {})
        full = storage is not None and storage["percent"] >= self.cfg["system"]["disk_warn_percent"]
        out["storage"] = (storage is not None and not full,
                          f"Storage almost full ({storage['percent']:.0f}%)" if full else "Storage not checked yet")
        return out

    def _ownership(self, now: float, nav_svc: Dict, autostart_on: bool) -> Optional[str]:
        """Two managers or two processes for AIO NAV (the configured nav service only)."""
        dup = self._duplicate.update(FAULT if nav_svc.get("duplicate") else HEALTHY, now) == FAULT
        if dup:
            return f"AIO NAV is running twice (pids {', '.join(map(str, nav_svc.get('pids') or []))})"
        unit = (nav_svc.get("unit_state") or {})
        if autostart_on and unit.get("file_state") == "enabled":
            return (f"{nav_svc.get('unit') or 'aio-nav.service'} also starts AIO NAV at boot; "
                    f"disable it (sudo systemctl disable {nav_svc.get('unit') or 'aio-nav.service'})")
        return None

    def _readiness(self, now, shown, nav, nav_svc, gq, fresh, latest, in_grace) -> Dict:
        rcfg = self.cfg["readiness"]
        required = set(rcfg.get("require") or [])
        if "dso" in required and rcfg.get("dso_only_with_vupt", True) and not self.cfg.aio_nav.get("vupt"):
            required.discard("dso")                 # DSO only matters when AIO NAV uses it (VUPT)
        conditions, listing = {}, []
        unknown_required = sorted(n for n in required if n not in rd.CONDITIONS)
        for name, (ok, why) in self._condition_inputs(gq).items():
            ready = self.conditions[name].update(ok, now)
            label = rd.CONDITIONS[name][2]
            reason = "" if ready else (why if not ok else f"{label}: waiting for it to stay stable")
            conditions[name] = {"required": name in required, "ok": bool(ok), "ready": ready, "reason": reason}
            listing.append({"name": name, "label": label, **conditions[name]})

        auto = (self.autostart_status() if self.autostart_status else None) or {"enabled": False}
        ever_ready = self._ready_run
        if not nav_svc.get("pids") and auto.get("ready_this_boot"):
            # A dashboard restart loses the per-process latch; the start-up coordinator keeps
            # "READY in this boot" on disk, so a crash after READY still asks for a safe stop.
            ever_ready = True
        inp = {
            "product_state": shown, "product_reasons": self._product_reasons, "step": self._step,
            "nav_running": bool(nav_svc.get("pids")), "nav_fresh": fresh, "nav_flags": latest,
            "ever_output": bool(latest), "ever_ready": ever_ready, "booting": in_grace,
            "conditions": conditions, "hold": bool(auto.get("hold")), "autostart": auto,
            "nav_required_flags": [f for f in self.cfg["nav"]["ready_requires"] if f in ALIGN_LAMPS],
            "wanted": process_control.wanted(self.cfg, self.cfg["nav"]["service"]),
            "unknown_required": unknown_required,
            "ownership": self._ownership(now, nav_svc, bool(auto.get("enabled"))),
            "config_ok": bool(self.cfg.aio_nav.get("loaded")),
        }
        snap = rd.evaluate(inp)
        if snap["state"] == "READY":
            self._ready_run = True
        if snap["state"] != self._rd_state:
            level = {"READY": "success", "FAULT": "fault", "DEGRADED": "warning"}.get(snap["state"], "info")
            if self._rd_state is not None:
                self.events.add(level, "readiness", f"Readiness {snap['state']}", "; ".join(snap["reasons"]))
            self._rd_state, self._rd_since = snap["state"], now
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        quality = gq.get("quality") if gq.get("available") else None
        code = {"none": 1, "spp": 2, "unrated_poor": 2, "float": 3, "unrated": 3, "fixed": 4}.get(quality, 0)
        snap.update({
            "schema_version": rd.SCHEMA_VERSION, "session_id": self.session_id, "sequence": self._sequence,
            "state_age_s": round(now - self._rd_since, 1),
            "observed_at": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "uptime_ms": int((now - self.started) * 1000),
            "gnss_quality": rd.GNSS_CODE_NAMES[code], "gnss_quality_code": code,
            "nav": {"age_ms": None if nav.get("age_s") is None else int(nav["age_s"] * 1000),
                    "rate_hz": round(nav.get("rate_hz") or 0.0, 1),
                    "flags_c": int(latest.get("flags_c", 0)) if fresh and latest else 0},
            "required": sorted(required), "conditions": listing,
            "autostart": {k: v for k, v in auto.items() if k != "enabled"} | {"enabled": bool(auto.get("enabled"))},
        })
        return snap

    def _health_view(self, shown: str, r: Dict) -> Dict:
        """What the pages show. The product state as before, except where readiness knows more:
        a required condition missing, degraded after READY, an operator stop, or a start-up the
        coordinator is waiting for. The pages and the vehicle (0x05) then never disagree."""
        state, lines, step = shown, list(self._product_reasons), self._step
        why = "; ".join(r["reasons"])
        if r["state"] == "DEGRADED" and shown in (READY, INITIALIZING):
            state, lines, step = DEGRADED, ["Navigation degraded: " + why, r["action_text"]], None
        elif r["state"] == "REFINING" and shown == READY:          # aligned, a required condition is missing
            state, lines, step = INITIALIZING, ["Waiting for: " + why, r["action_text"]], None
        elif r["state"] == "FAULT" and "OWNERSHIP_CONFLICT" in r["inhibit"]:
            state, lines, step = PRODUCT_FAULT, [why, "Maintenance is needed before AIO NAV can be used."], None
        elif r["state"] == "FAULT" and r["requested_action"] == "SAFE_STOP" and shown != PRODUCT_FAULT:
            state, lines, step = PRODUCT_FAULT, [why, r["action_text"]], None
        elif r["state"] == "FAULT" and "STARTUP_TIMEOUT" in r["inhibit"] and shown == STOPPED:
            state, lines, step = PRODUCT_FAULT, [why, "Automatic start gave up. Press Start to try again."], None
        elif r["state"] == "CHECKING" and shown in (STOPPED, PRODUCT_FAULT, PRODUCT_UNKNOWN):
            state, lines, step = STARTING, ["Starting automatically", why], None
        elif r["state"] == "MAINTENANCE" and "MAINTENANCE_HOLD" in r["inhibit"]:
            lines = ["Stopped by an operator", "Press Start to resume."]
        return {"state": state, "since_s": self._clock() - self.product.since,
                "title": lines[0] if lines else "", "detail": lines[1] if len(lines) > 1 else "",
                "reasons": [x for x in lines if x], "step": step,
                "action": r["requested_action"], "readiness": r["state"]}

    @staticmethod
    def _gnss_detail(gq: Dict) -> str:
        s = gq.get("sigma_h_m")
        if gq.get("available") and gq.get("quality") == "none":
            return gq.get("reason") or ""
        if not gq.get("available") or s is None:
            return ""
        return f"horizontal accuracy {s * 100:.0f} cm" if s < 1 else f"horizontal accuracy {s:.1f} m"

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
            # AIO NAV stopped on purpose (nobody asked it to run) is a normal state, not an issue.
            if key == nav_key and state == "stopped" and product_state == STOPPED:
                continue
            if key == nav_key:
                unit = svc.get("unit") or "aio-nav.service"
                if svc.get("duplicate"):
                    issue("services", FAULT, f"{svc.get('label', key)} is running twice",
                          "pids " + ", ".join(map(str, svc.get("pids") or [])), "system")
                if self.cfg["nav"]["allow_control"] and (svc.get("unit_state") or {}).get("file_state") == "enabled":
                    issue("services", WARNING, f"{unit} starts AIO NAV at boot, and the dashboard also controls it",
                          f"one manager only: sudo systemctl disable {unit}", "system")
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
