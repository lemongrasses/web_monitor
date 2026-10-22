"""Dashboard configuration: built-in defaults deep-merged with a YAML file."""

import copy
import json
import glob
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "product": {"host": "0.0.0.0", "port": 8080,
                # Setup page: the lever arms of the Live config file may be changed there
                # (no password, like Start / Stop; checked, backed up and logged).
                "edit_lever_arms": True},
    "maintenance": {"host": "0.0.0.0", "port": 8081,
                    # The config editor and the terminal need this password (aio-dashboard password).
                    "password_file": "config/maint_password", "unlock_minutes": 15,
                    "lock": "all",        # all: the whole maintenance view needs it; tools: only those two
                    # Before an AIO NAV config file is saved, start AIO NAV on it for a moment, cut
                    # off from the network, and refuse the file if it stops (actions/aionav_config).
                    "config_test_start": True},
    # Browsers allowed to open the pages (IPs or CIDRs). Loopback is always allowed.
    # Empty = no restriction.
    "access": {"allowed_clients": []},
    "nav": {
        "udp_bind": "127.0.0.1:9000",
        "aio_nav_config": "auto",  # "auto": search the usual aio-nav-ros install locations
        "expected_rate_hz": None,  # None: take output_rate from aio_nav_config
        "service": "aio_nav",
        "allow_control": False,    # True: the Overview page can start/stop AIO NAV (+ DSO)
        "control_group": ["aio_nav", "dso"],   # what Start/Stop acts on, in start order
        "startup_grace_s": 3.0,
        "startup_timeout_s": 60.0,   # after AIO NAV starts: no output for this long is a fault
        "stale_warn_s": 0.5,
        "stale_fault_s": 2.0,
        "low_rate_ratio": 0.8,
        "gnss_timeout_s": 3.0,
        "flag_active_s": 1.0,
        "ready_requires": ["alignment", "heading_valid", "fine_alignment"],
        "trajectory": {"recent_window_s": 60.0, "recent_hz": 10.0, "older_hz": 1.0},
    },
    "services": {},
    "devices": {},
    "network": {"sensor_interface": "auto", "ping_interval_s": 5.0},
    "system": {"interval_s": 2.0, "disk_paths": ["/"], "disk_warn_percent": 90, "temp_warn_c": 85},
    "ros": {"enabled": True, "graph_interval_s": 2.0, "rate_window_s": 2.0, "nodes": [], "topics": [],
            # Modes: the active one is chosen on Maintenance > ROS 2 (stored in state/ros_mode.json) or
            # by ros.mode here. Each mode names an aio-nav-ros config file (in the folder of the
            # normal aio_nav.yaml); AIO NAV and DSO are started with it, and its ros_domain_id /
            # ros_localhost_only become the dashboard's own ROS environment. A mode may also set
            # domain_id / localhost_only itself to override the file. mode: null keeps the environment.
            "mode": None,
            # Restart the dashboard when its ROS connection is stuck (sensor drivers running in the
            # same domain, yet no node visible for isolation_grace_s). At most once per cooldown.
            "isolation_restart": False, "isolation_grace_s": 30, "isolation_cooldown_s": 600,
            # Topics expected at >= above_hz are listened to only window_s out of every period_s (the
            # rate and freshness shown are the last window's). A 100 Hz topic otherwise costs a
            # Python callback per message, about 12% of a core. Only this dashboard's own
            # subscriptions change. GNSS, DSO odometry and the camera/LiDAR previews are separate.
            "sampling": {"enabled": False, "period_s": 5.0, "window_s": 1.5, "above_hz": 30.0},
            "modes": {"live": {"label": "Live", "config": "aio_nav.yaml"},
                      "bag": {"label": "Bag replay", "config": "aio_nav_bag.yaml"}}},
    "data": {"roots": "auto"},  # "auto": the aio-nav-ros output folder
    # Optional modules (see modules.py), e.g. {replay: {enabled: false}}. A module is on when it is
    # installed, unless switched off here.
    "modules": {},
    # Receiver quality lamp (RTK fix / RTK float / SPP / no signal) from this NavSatFix topic.
    "gnss": {"topic": "/openrtk330/gnss/fix", "timeout_s": 3.0},
    # Watches DSO's odometry and restarts DSO (only DSO) when it turns NaN.
    "dso_watchdog": {"enabled": False, "topic": "/dso/odometry", "service": "dso",
                     "bad_messages": 1,       # NaN messages in a row before restarting (1 = at once)
                     "cooldown_s": 2.0,       # minimum gap between two NaN restarts
                     "settle_s": 3.0,         # ignore messages right after a restart
                     "retry_s": 30.0,         # DSO should run but does not: try again this often ...
                     "retry_max_s": 300.0,    # ... doubling while it keeps exiting at once, up to this
                     "stay_up_s": 120.0,      # running this long: the waits start afresh
                     "fast_restarts": 3,      # this many restarts within window_s ...
                     "window_s": 60.0,        # ... and further restarts wait retry_s too
                     "max_memory_mb": "auto"},  # RAM + swap above this: restart DSO. auto: 20% of RAM; 0: off
    # READY for the vehicle (state/readiness.py). By default READY needs only AIO NAV aligned with
    # fresh output, as before. require: extra conditions, each held stable_s before READY counts
    # and lost after failing loss_s: imu, gnss, gnss_quality, dso, vehicle_route, storage.
    "readiness": {"require": [], "stable_s": 2.0, "loss_s": 1.0,
                  # RTK: READY needs a GNSS position of at least gnss_min_quality (gnss + gnss_quality).
                  # Switchable on Maintenance > Overview (kept in state/readiness.json).
                  "require_rtk": True,
                  "gnss_min_quality": "float",   # for gnss_quality: spp, float or fixed
                  "gnss_stable_s": 3.0,
                  "gnss_loss_s": 3.0,            # GNSS lost this long before READY drops (trees, bridges)
                  "imu_topic": "auto",           # a ros.topics name; auto: the first one in group nav
                  "dso_stale_s": 1.0,            # DSO odometry older than this is not ready
                  # "Vehicle still" from the IMU (2 s of samples): largest rotation rate and the
                  # spread of the acceleration magnitude. Parked with the engine on: < 0.035 rad/s,
                  # < 0.16 m/s2; driving: > 0.17 rad/s, > 0.6 m/s2 (recorded drive, 2026-09-23).
                  # Plus, while the receiver has a GNSS position, its speed (still_speed_topic: the
                  # OpenRTK330's INS odometry; "" = IMU only). These are the defaults: limits measured
                  # on the vehicle (Maintenance > Still detection) replace them (state/still.py).
                  "still_gyro_rad_s": 0.05, "still_accel_std": 0.15, "still_speed_m_s": 0.3,
                  "still_speed_topic": "/openrtk330/odometry"},
    # Run AIO NAV (+ DSO, the nav.control_group) without an operator: start after power-on, restart
    # after failures, never give up (actions/autostart.py). Needs nav.allow_control.
    "autostart": {"enabled": True,
                  "prerequisites": ["imu"],      # readiness conditions to wait for: imu, gnss
                  "prerequisite_stable_s": 2.0,
                  "prerequisite_timeout_s": 120.0,  # still missing after this: a fault (it keeps waiting)
                  "start_services": [],          # user units to start if not running, e.g. [drivers]
                  "retry_backoff_s": [5, 15, 30, 60],   # waits between attempts; the last repeats
                  "max_attempts": 0,             # 0 = never give up
                  "stale_grace_s": 10.0,         # output stopped, inputs fine: restart after this
                  "require_still": True,         # start only with the vehicle still (a reboot or
                                                 # a crash may happen while driving)
                  "still_stable_s": 5.0},
    # System clock from GNSS time (collectors/gnss_time.py): the IMU stamps go to chrony as a time
    # source (refclock SHM, set up by install.sh). latency_ms: delay of the fastest IMU message
    # after its stamp (6.2 h against NTP: 4.6 ms median). AIO NAV starts only once the clock matches GNSS time
    # within clock_tolerance_s, waiting at most clock_wait_s (then it starts anyway).
    "gnss_time": {"enabled": True, "latency_ms": 4.6, "shm_unit": 2,
                  "clock_tolerance_s": 1.0, "clock_wait_s": 120.0},
    # Restart the sensor driver unit when it runs but its IMU sends nothing (Live mode only).
    "driver_watchdog": {"enabled": True,
                        "service": "auto",       # a services key with user_unit; auto: drivers_service()
                        "stale_s": 45.0,         # IMU missing/stale this long, unit active: restart
                        "retry_backoff_s": [60, 120, 300]},   # between restarts; the last repeats
    # 0x05 status packet (docs/OPERATION.md): readiness for the vehicle, next to the 0x04 data.
    "status_udp": {"enabled": True,
                   "destination": "auto",       # host:port; auto: output_udp of aio_nav.yaml
                   "message_id": 5, "rate_hz": 2.0},
    # Wi-Fi on the user Settings page and Maintenance > Network (actions/wifi.py). Changes need a
    # browser connected by cable (plus the password on Maintenance), and once:
    # sudo deploy/install.sh --with-wifi (no root afterwards).
    "wifi": {"enabled": True, "interface": "auto", "connect_timeout_s": 30, "ip_timeout_s": 30,
             # Hotspot (Maintenance > Network): the Wi-Fi becomes an access point for phones and
             # tablets instead of joining a network (one radio: it cannot do both). enabled "auto":
             # when the installer's "hotspot" option is installed. Phones get addresses in the /24 of
             # `address` and may open the pages (allow_clients); band "bg" = 2.4 GHz. relay_udp: each
             # device on it gets a copy of the NAV and 0x05 packets on relay_port (the vehicle's
             # output is unchanged); switchable on Maintenance > Network.
             "hotspot": {"enabled": "auto", "address": "10.42.0.1/24", "band": "bg", "allow_clients": True,
                         "relay_udp": True, "relay_port": 9000}},
    "events": {"log_file": "logs/events.jsonl", "max_memory": 500},
    "actions": {"restart_timeout_s": 30.0, "diagnostic_timeout_s": 15.0,
                "stop_grace_s": 5.0},  # stop_grace_s: SIGTERM -> SIGKILL delay for AIO NAV / DSO
    "fake": {"enabled": False, "state_file": "dev/fake_state.json"},
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def resolve_path(p: str) -> Path:
    """Expand ~ and resolve relative paths against the project root."""
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else PROJECT_ROOT / path


INSTALLED_OPTIONS = "config/installed-options"


def installed_options() -> set:
    """The optional parts chosen in the installer: deploy/install.sh writes their names to
    config/installed-options (one line, "#" comments). Empty when run from a source checkout."""
    try:
        text = resolve_path(INSTALLED_OPTIONS).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {w for line in text.splitlines() if not line.lstrip().startswith("#") for w in line.split()}


def drivers_service(cfg) -> Optional[str]:
    """The services key of the sensor driver stack (camera and IMU / GNSS together): "drivers" when
    it has a user_unit, else the first service with one. A single sensor can have its own user unit
    too (the camera's point-lab-sensor@basler.service); it is never taken for the stack."""
    services = cfg["services"]
    if (services.get("drivers") or {}).get("user_unit"):
        return "drivers"
    return next((k for k, s in services.items() if s.get("user_unit")), None)


def parse_host_port(value: str, default_host: str = "127.0.0.1") -> Optional[tuple]:
    if not value or ":" not in value:
        return None
    host, _, port = value.rpartition(":")
    try:
        port_i = int(port)
    except ValueError:
        return None
    if not 0 < port_i < 65536:
        return None
    if host in ("", "localhost"):
        host = default_host
    return host, port_i


# Where aio-nav-ros puts aio_nav.yaml: a colcon install in a clone, or a release prefix
# (scripts/deploy_release.sh --prefix ~/.local/opt/aio-nav-ros).
AIO_NAV_CONFIG_GLOBS = (
    "/home/*/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml",
    "/home/*/*/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml",
    "/home/*/.local/opt/aio-nav-ros/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml",
    "/root/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml",
    "/opt/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml",
)


def discover_aio_nav_config() -> str:
    """Newest aio_nav.yaml in the usual install locations, or "" if none is found."""
    env = os.environ.get("AIO_NAV_CONFIG")
    if env and os.path.isfile(env):
        return env
    found = [f for pattern in AIO_NAV_CONFIG_GLOBS for f in glob.glob(pattern)]
    return max(found, key=os.path.getmtime) if found else ""


def aio_nav_output_dir(config_path: str, fusion_txt_path: str = "") -> Optional[str]:
    """Folder where aio_nav_node writes its logs.

    An absolute fusion_txt_path wins. Otherwise logs go to output/ in the aio-nav-ros
    tree (the folder that contains install/, or the release prefix).
    """
    if fusion_txt_path and os.path.isabs(fusion_txt_path):
        return os.path.dirname(fusion_txt_path)
    if not config_path:
        return None
    p = Path(config_path).resolve()
    for parent in p.parents:
        if (parent / "output").is_dir():
            return str(parent / "output")
    for parent in p.parents:
        if parent.name == "install" or parent.name == "aio-nav-ros":
            root = parent.parent if parent.name == "install" else parent
            return str(root / "output")
    return None


_PARAMS_CACHE: Dict[Any, Dict[str, Any]] = {}


def read_aio_nav_params(path: str) -> Dict[str, Any]:
    """Read output_udp / output_rate / ROS domain ... from an aio_nav.yaml (ROS 2 params file).

    The parsed result is cached by file (path, modification time, size): the pure-Python YAML
    parser costs tens of milliseconds, and this is asked for several times a second. Editing the
    file changes its time or size, so the next call parses it again."""
    p = Path(discover_aio_nav_config() if path in ("", "auto", None) else str(resolve_path(path)))
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size, path in ("", "auto", None))
    except OSError:
        key = None
    if key is not None and key in _PARAMS_CACHE:
        return copy.deepcopy(_PARAMS_CACHE[key])
    result = _read_aio_nav_params(path)
    if key is not None:
        for old in [k for k in _PARAMS_CACHE if k[0] == key[0]]:
            del _PARAMS_CACHE[old]          # drop the outdated version of this file
        _PARAMS_CACHE[key] = copy.deepcopy(result)
    return result


def _read_aio_nav_params(path: str) -> Dict[str, Any]:
    """Uncached reader behind read_aio_nav_params."""
    auto = path in ("", "auto", None)
    if auto:
        path = discover_aio_nav_config()
    result: Dict[str, Any] = {"path": path, "auto": auto, "loaded": False, "output_udp": "",
                              "output_rate": None, "fusion_txt_path": "", "ros_domain_id": None,
                              "ros_localhost_only": None, "use_sim_time": None, "vupt": False}
    if not path:
        result["error"] = "aio_nav.yaml not found in the usual locations; set nav.aio_nav_config"
        return result
    p = resolve_path(path)
    result["path"] = str(p)
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        result["error"] = str(e)
        return result
    for section in doc.values():
        params = section.get("ros__parameters") if isinstance(section, dict) else None
        if isinstance(params, dict):
            result["loaded"] = True
            result["output_udp"] = str(params.get("output_udp") or "")
            rate = params.get("output_rate")
            result["output_rate"] = float(rate) if isinstance(rate, (int, float)) else None
            result["fusion_txt_path"] = str(params.get("fusion_txt_path") or "")
            for key in ("ros_domain_id", "ros_localhost_only"):
                v = params.get(key)
                result[key] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            if isinstance(params.get("use_sim_time"), bool):
                result["use_sim_time"] = params["use_sim_time"]
            odo = params.get("Odometry")
            result["vupt"] = bool(odo.get("vupt")) if isinstance(odo, dict) else False
            break
    return result


class Config:
    AIO_NAV_MAX_AGE_S = 2.0      # how long a read of aio_nav.yaml is reused (Start / Restart always re-read)

    problems: List[str] = []                         # settings replaced by defaults (load_config)

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self.data = data
        self.source = source
        self._aio_nav: Optional[Dict[str, Any]] = None
        self._aio_nav_at = 0.0
        self._roots_auto = data["data"].get("roots") in ("auto", None)
        if self._roots_auto:
            data["data"]["roots"] = self.auto_data_roots()
        else:
            data["data"]["roots"] = self._normalise_roots(data["data"].get("roots"))

    def _normalise_roots(self, roots) -> List[Dict[str, Any]]:
        """data.roots: a list of folders, each a path or {path, name, id}. A plain path is the
        natural way to write it (it used to crash the dashboard); anything else is skipped."""
        out = []
        for r in roots if isinstance(roots, list) else [roots]:
            if isinstance(r, str) and r:
                out.append({"path": r})
            elif isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"]:
                out.append(r)
            else:
                self.problems = self.problems + [f"data.roots: {r!r} is not a folder; skipped"]
        return out

    @property
    def aio_nav(self) -> Dict[str, Any]:
        """Parameters of the active aio-nav-ros config file. Re-read when it is older than
        AIO_NAV_MAX_AGE_S, and the file is only parsed again when it changed on disk, so editing
        aio_nav.yaml takes effect without restarting the dashboard."""
        now = time.monotonic()
        if self._aio_nav is None or now - self._aio_nav_at > self.AIO_NAV_MAX_AGE_S:
            params = read_aio_nav_params(self._aio_nav_path())
            if self.data["nav"].get("aio_nav_config", "auto") in ("", "auto", None):
                params["auto"] = True  # found in the install folder, not named in the config
            self._aio_nav, self._aio_nav_at = params, now
        return self._aio_nav

    def refresh_aio_nav(self) -> Dict[str, Any]:
        """Read aio_nav.yaml now (before starting AIO NAV, so it starts with what is on disk)."""
        self._aio_nav = None
        if self._roots_auto:
            self.data["data"]["roots"] = self.auto_data_roots()
        return self.aio_nav

    def auto_data_roots(self) -> List[Dict[str, Any]]:
        out = aio_nav_output_dir(self.aio_nav.get("path", ""), self.aio_nav.get("fusion_txt_path", ""))
        return [{"id": "aio-nav-logs", "name": "AIO NAV logs", "path": out}] if out else []

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def fake(self) -> bool:
        return bool(self.data["fake"]["enabled"])

    @property
    def expected_nav_rate(self) -> Optional[float]:
        rate = self.data["nav"].get("expected_rate_hz")
        return float(rate) if rate else self.aio_nav.get("output_rate")

    # ---- ROS mode -> aio-nav-ros config file
    def _aio_nav_base(self) -> str:
        """The normal aio_nav.yaml (configured, or found in the aio-nav-ros install folder)."""
        configured = self.data["nav"].get("aio_nav_config", "auto")
        return discover_aio_nav_config() if configured in ("", "auto", None) else str(resolve_path(configured))

    def mode_config_path(self, mode: Optional[str] = None) -> Optional[str]:
        """Path of the aio-nav-ros config file belonging to a mode (default: the active one)."""
        mode = mode if mode is not None else self.ros_mode()
        name = (self.data["ros"]["modes"].get(mode) or {}).get("config") if mode else None
        base = self._aio_nav_base()
        if not name or not base:
            return None
        cand = Path(base).resolve().parent / name
        return str(cand) if cand.is_file() else None

    def _aio_nav_path(self) -> str:
        return self.mode_config_path() or self.data["nav"].get("aio_nav_config", "auto")

    # ---- ROS environment mode (live / bag replay)
    def state_file(self, name: str) -> Path:
        return resolve_path(f"state/{name}")

    def ros_mode_file(self) -> Path:
        return self.state_file("ros_mode.json")

    def module_on(self, name: str) -> bool:
        from . import modules
        return modules.enabled(self.data, name)

    def ros_mode(self) -> Optional[str]:
        """Active preset: the one chosen on the maintenance page, else ros.mode, else None.

        Choosing a mode belongs to the optional replay module; without it a choice left on disk is
        ignored, so a product machine always runs the configured mode (live). A choice other than the
        configured mode lasts until the next power-on: a device left in Bag replay would otherwise
        wait for bag data for ever after being switched on in the field."""
        modes = self.data["ros"]["modes"]
        configured = self.data["ros"].get("mode")
        chosen = None
        if self.module_on("replay"):
            try:
                saved = json.loads(self.ros_mode_file().read_text(encoding="utf-8"))
                chosen = saved.get("mode")
                if chosen != configured and saved.get("boot_id") != _boot_id():
                    chosen = None                # chosen before this power-on
            except (OSError, ValueError, AttributeError):
                chosen = None
        if chosen in modes:
            return chosen
        return configured if configured in modes else None

    def save_ros_mode(self, mode: str) -> None:
        if mode not in self.data["ros"]["modes"]:
            raise ValueError(f"unknown ROS mode {mode!r}")
        f = self.ros_mode_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps({"mode": mode, "boot_id": _boot_id()}) + "\n", encoding="utf-8")
        tmp.replace(f)
        self._aio_nav = None                     # the mode decides which config file is read

    def _mode_ros(self, mode: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Domain / localhost-only of a mode: its own override, else the values in its config file."""
        m = self.data["ros"]["modes"][mode]
        domain = m.get("domain_id") if m.get("domain_id") is not None else params.get("ros_domain_id")
        local = m.get("localhost_only") if m.get("localhost_only") is not None else params.get("ros_localhost_only")
        env: Dict[str, str] = {}
        if domain is not None:
            env["ROS_DOMAIN_ID"] = str(int(domain))
        if local is not None:
            env["ROS_LOCALHOST_ONLY"] = "1" if int(local) else "0"
        return {"domain_id": None if domain is None else int(domain),
                "localhost_only": None if local is None else bool(int(local)), "env": env}

    def ros_env(self) -> Dict[str, str]:
        """ROS_DOMAIN_ID / ROS_LOCALHOST_ONLY of the active mode ({} = keep the environment)."""
        mode = self.ros_mode()
        return {} if mode is None else self._mode_ros(mode, self.aio_nav)["env"]

    def ros_mode_info(self) -> Dict[str, Any]:
        mode = self.ros_mode()
        modes = self.data["ros"]["modes"]
        listed = []
        for name, m in modes.items():
            path = self.mode_config_path(name)
            params = read_aio_nav_params(path) if path else {}
            ros = self._mode_ros(name, params)
            listed.append({"name": name, "label": m["label"], "config": m.get("config") or "",
                           "found": bool(path), "domain_id": ros["domain_id"],
                           "localhost_only": ros["localhost_only"],
                           "use_sim_time": params.get("use_sim_time")})
        cur = next((m for m in listed if m["name"] == mode), None)
        return {
            "mode": mode,
            "label": cur["label"] if cur else "Environment",
            "config": cur["config"] if cur else "",
            "domain_id": cur["domain_id"] if cur else None,
            "localhost_only": cur["localhost_only"] if cur else None,
            "modes": listed,
        }

    def launcher(self, name: str) -> Optional[str]:
        """Path of an aio-nav-ros wrapper (aio-nav, aio-nav-dso) from the install/ folder."""
        if os.path.isabs(name):
            return name if os.access(name, os.X_OK) else None
        path = self.aio_nav.get("path")
        if not path:
            return None
        for parent in Path(path).resolve().parents:
            cand = parent / "lib" / "aio_nav_ros" / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        return None

    def nav_destinations(self) -> List[Dict[str, Any]]:
        """Configured NAV UDP outputs (display only; v1 does not edit them)."""
        bind = parse_host_port(self.data["nav"]["udp_bind"])
        dests = []
        extra = parse_host_port(self.aio_nav.get("output_udp", ""))
        if extra and extra != ("127.0.0.1", 9000):
            dests.append({"host": extra[0], "port": extra[1], "kind": "external"})
        dests.append({"host": "127.0.0.1", "port": 9000, "kind": "local",
                      "monitored": bool(bind and bind[1] == 9000)})
        return dests

    def service_label(self, key: str) -> str:
        return self.data["services"].get(key, {}).get("label", key)


def _kind_ok(value: Any, default: Any) -> bool:
    if default is None or default == "auto":
        return True                                  # free: a value, a list, "auto", ...
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return value is None or isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def _sanitize(data: Dict, defaults: Dict, prefix: str = "") -> List[str]:
    """Replace values of the wrong kind (text where a number belongs, ...) by the default, in place.
    A valid YAML file with one wrong value must not stop the fully automatic system: it would fail
    later, in a loop, and the systemd watchdog would restart the dashboard for ever."""
    problems = []
    for key, default in defaults.items():
        if key not in data:
            continue
        name = f"{prefix}{key}"
        value = data[key]
        if not _kind_ok(value, default):
            data[key] = copy.deepcopy(default)
            problems.append(f"{name}: {value!r} is not a valid value; using the default {default!r}")
        elif isinstance(default, dict) and default:
            problems += _sanitize(value, default, name + ".")
    return problems


def load_config(path: Optional[str], force_fake: bool = False) -> Config:
    data = copy.deepcopy(DEFAULTS)
    source = None
    if path:
        source = resolve_path(path)
        with open(source, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{source}: the settings must be a YAML mapping (key: value)")
        data = _deep_merge(data, loaded)
    problems = _sanitize(data, DEFAULTS)
    for p in problems:
        logger.error("settings: %s", p)
    if force_fake:
        data["fake"]["enabled"] = True
    cfg = Config(data, source)
    cfg.problems = problems + cfg.problems
    if cfg.aio_nav.get("loaded"):
        logger.info("aio_nav config: %s%s", cfg.aio_nav["path"], " (found automatically)" if cfg.aio_nav["auto"] else "")
    else:
        logger.warning("aio_nav config not loaded (%s): %s", cfg.aio_nav.get("path"),
                       cfg.aio_nav.get("error", "no path or no ros__parameters"))
    return cfg


# The settings that last started the dashboard (state/ is writable by the dashboard's user, the
# config folder is not). A settings file with an error must not stop the fully automatic system:
# the dashboard then starts with this copy, or with the built-in defaults, and says so.
LAST_GOOD = "state/dashboard.lastgood.yaml"


def fallback_config(force_fake: bool = False) -> Tuple[Config, str]:
    """The last good settings, else the built-in defaults; and which of the two."""
    alt = resolve_path(LAST_GOOD)
    if alt.exists():
        try:
            return load_config(str(alt), force_fake), f"the last good settings ({alt})"
        except Exception as e:                       # noqa: BLE001 - any error: use the defaults
            logger.error("last good settings %s unusable: %s", alt, e)
    return load_config(None, force_fake), "the built-in defaults"


def load_config_safely(path: Optional[str], force_fake: bool = False) -> Tuple[Config, str]:
    """(config, problem): problem is "" when the settings file was used."""
    try:
        return load_config(path, force_fake), ""
    except Exception as e:                           # noqa: BLE001 - YAML, I/O, structure
        cfg, which = fallback_config(force_fake)
        problem = f"settings file {path} unusable ({e}); running with {which}"
        logger.error("%s", problem)
        return cfg, problem


def remember_good(cfg: Config) -> None:
    """Keep a copy of the settings file that just started the dashboard."""
    if cfg.source is None:
        return
    try:
        text = Path(cfg.source).read_text(encoding="utf-8")
        dest = resolve_path(LAST_GOOD)
        if dest.exists() and dest.read_text(encoding="utf-8") == text:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(dest)
    except OSError as e:
        logger.warning("cannot keep a copy of the good settings: %s", e)
