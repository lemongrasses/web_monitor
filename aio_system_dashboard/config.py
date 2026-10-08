"""Dashboard configuration: built-in defaults deep-merged with a YAML file."""

import copy
import json
import glob
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: Dict[str, Any] = {
    "product": {"host": "0.0.0.0", "port": 8080},
    "maintenance": {"host": "0.0.0.0", "port": 8081},
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
                     "retry_s": 30.0,         # DSO should run but does not: try again this often
                     "fast_restarts": 3,      # this many restarts within window_s ...
                     "window_s": 60.0},       # ... and further restarts wait retry_s too
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


def resolve_path(p: str) -> Path:
    """Expand ~ and resolve relative paths against the project root."""
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else PROJECT_ROOT / path


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
                              "ros_localhost_only": None, "use_sim_time": None}
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
            break
    return result


class Config:
    AIO_NAV_MAX_AGE_S = 2.0      # how long a read of aio_nav.yaml is reused (Start / Restart always re-read)

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self.data = data
        self.source = source
        self._aio_nav: Optional[Dict[str, Any]] = None
        self._aio_nav_at = 0.0
        self._roots_auto = data["data"].get("roots") in ("auto", None)
        if self._roots_auto:
            data["data"]["roots"] = self.auto_data_roots()

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
        ignored, so a product machine always runs the configured mode (live)."""
        modes = self.data["ros"]["modes"]
        chosen = None
        if self.module_on("replay"):
            try:
                chosen = json.loads(self.ros_mode_file().read_text(encoding="utf-8")).get("mode")
            except (OSError, ValueError, AttributeError):
                chosen = None
        if chosen in modes:
            return chosen
        configured = self.data["ros"].get("mode")
        return configured if configured in modes else None

    def save_ros_mode(self, mode: str) -> None:
        if mode not in self.data["ros"]["modes"]:
            raise ValueError(f"unknown ROS mode {mode!r}")
        f = self.ros_mode_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"mode": mode}) + "\n", encoding="utf-8")
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


def load_config(path: Optional[str], force_fake: bool = False) -> Config:
    data = copy.deepcopy(DEFAULTS)
    source = None
    if path:
        source = resolve_path(path)
        with open(source, encoding="utf-8") as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})
    if force_fake:
        data["fake"]["enabled"] = True
    cfg = Config(data, source)
    if cfg.aio_nav.get("loaded"):
        logger.info("aio_nav config: %s%s", cfg.aio_nav["path"], " (found automatically)" if cfg.aio_nav["auto"] else "")
    else:
        logger.warning("aio_nav config not loaded (%s): %s", cfg.aio_nav.get("path"),
                       cfg.aio_nav.get("error", "no path or no ros__parameters"))
    return cfg
