"""Dashboard configuration: built-in defaults deep-merged with a YAML file."""

import copy
import glob
import logging
import os
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
        "startup_grace_s": 3.0,
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
    "ros": {"enabled": True, "graph_interval_s": 2.0, "rate_window_s": 2.0, "nodes": [], "topics": []},
    "data": {"roots": "auto"},  # "auto": the aio-nav-ros output folder
    "events": {"log_file": "logs/events.jsonl", "max_memory": 500},
    "actions": {"restart_timeout_s": 30.0, "diagnostic_timeout_s": 15.0},
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


def read_aio_nav_params(path: str) -> Dict[str, Any]:
    """Read output_udp / output_rate from an aio_nav.yaml (ROS 2 params file)."""
    auto = path in ("", "auto", None)
    if auto:
        path = discover_aio_nav_config()
    result: Dict[str, Any] = {"path": path, "auto": auto, "loaded": False, "output_udp": "",
                              "output_rate": None, "fusion_txt_path": ""}
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
            break
    return result


class Config:
    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self.data = data
        self.source = source
        self.aio_nav = read_aio_nav_params(data["nav"].get("aio_nav_config", "auto"))
        if data["data"].get("roots") in ("auto", None):
            out = aio_nav_output_dir(self.aio_nav.get("path", ""), self.aio_nav.get("fusion_txt_path", ""))
            data["data"]["roots"] = ([{"id": "aio-nav-logs", "name": "AIO NAV logs", "path": out}]
                                     if out else [])

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def fake(self) -> bool:
        return bool(self.data["fake"]["enabled"])

    @property
    def expected_nav_rate(self) -> Optional[float]:
        rate = self.data["nav"].get("expected_rate_hz")
        return float(rate) if rate else self.aio_nav.get("output_rate")

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
