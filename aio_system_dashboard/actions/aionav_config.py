"""Edit the aio-nav-ros config files (aio_nav.yaml, aio_nav_bag.yaml) from the Maintenance page.

* Only the config files of the configured modes can be opened; the page names a mode, never a path.
* The form changes values in place, line by line, so comments and layout stay as they are. The raw
  editor saves the text as typed. Either way the result must parse, keep `ros__parameters`, and
  pass the checks below before it is written.
* Every save keeps the previous version in state/aio_nav_backups/ (the last 30 per file) and
  refuses to overwrite a file that changed on disk since it was opened.
"""

import difflib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

MAX_BACKUPS = 30

# The settings offered as a form. path: keys below ros__parameters. Anything else: raw editor.
FIELDS: List[Dict[str, Any]] = [
    {"group": "Output", "path": ["output_udp"], "label": "UDP destination", "type": "hostport",
     "help": "Extra target for the NAV packets, host:port (the external computer). Empty: this device only."},
    {"group": "Output", "path": ["output_rate"], "label": "Output rate (Hz)", "type": "float", "min": 1, "max": 1000,
     "help": "How often a navigation solution is sent."},
    {"group": "ROS", "path": ["ros_domain_id"], "label": "ROS domain", "type": "int", "min": 0, "max": 232,
     "help": "Must match the sensor drivers (live) or the bag player (bag)."},
    {"group": "ROS", "path": ["ros_localhost_only"], "label": "This computer only", "type": "choice",
     "choices": [[1, "Yes (1)"], [0, "No, network (0)"]], "help": "ROS_LOCALHOST_ONLY."},
    {"group": "ROS", "path": ["use_sim_time"], "label": "Use sim time", "type": "bool",
     "help": "On for bag replay (follows /clock), off for live sensors."},
    {"group": "ROS", "path": ["imu_topic"], "label": "IMU topic", "type": "topic", "help": ""},
    {"group": "ROS", "path": ["gnss_fix_topic"], "label": "GNSS fix topic", "type": "topic", "help": ""},
    {"group": "ROS", "path": ["odom_topic"], "label": "Odometry topic (DSO)", "type": "topic", "help": ""},
    {"group": "Logs", "path": ["save_fusion_txt"], "label": "Save navigation log", "type": "bool", "help": ""},
    {"group": "Logs", "path": ["save_parsed_txt"], "label": "Save IMU / GNSS / odometry logs", "type": "bool", "help": ""},
    {"group": "Logs", "path": ["fusion_txt_path"], "label": "Log file", "type": "str",
     "help": "The other logs are named after it. Relative paths are under the folder AIO NAV runs in."},
    {"group": "Alignment", "path": ["Alignment", "coarse_duration"], "label": "Coarse alignment (s)",
     "type": "float", "min": 0, "max": 3600, "help": "Time standing still for leveling."},
    {"group": "Alignment", "path": ["Alignment", "fine_duration"], "label": "Fine alignment limit (s)",
     "type": "float", "min": 0, "max": 36000, "help": "Fine alignment ends at the latest after this time."},
    {"group": "Alignment", "path": ["Alignment", "fine_heading_std_threshold"], "label": "Fine heading target (°)",
     "type": "float", "min": 0, "max": 180, "help": "Fine alignment ends when heading accuracy reaches this."},
    {"group": "Alignment", "path": ["Alignment", "pos", "option"], "label": "Initial position option",
     "type": "int", "min": 0, "max": 9, "help": "Alignment.pos.option (see the navigation team's notes)."},
    {"group": "Alignment", "path": ["Alignment", "pos", "ini_value"], "label": "Initial position (lat, lon, h)",
     "type": "list3", "help": "Alignment.pos.ini_value: degrees, degrees, metres."},
    {"group": "Alignment", "path": ["Alignment", "att", "option"], "label": "Initial attitude option",
     "type": "int", "min": 0, "max": 9, "help": "Alignment.att.option: 2 takes the heading from the value below "
                                                "instead of from driving."},
    {"group": "Alignment", "path": ["Alignment", "att", "ini_value"], "label": "Initial attitude (roll, pitch, heading)",
     "type": "list3", "help": "Alignment.att.ini_value, degrees."},
    {"group": "Aiding", "path": ["Motion", "zupt"], "label": "ZUPT", "type": "bool", "help": "Zero-velocity updates."},
    {"group": "Aiding", "path": ["Motion", "zihr"], "label": "ZIHR", "type": "bool", "help": "Zero heading-rate updates."},
    {"group": "Aiding", "path": ["Motion", "nhc"], "label": "NHC", "type": "bool", "help": "Non-holonomic constraint."},
    {"group": "Aiding", "path": ["Odometry", "vupt"], "label": "VUPT", "type": "bool", "help": "Visual odometry updates (DSO)."},
    {"group": "Lever arms", "path": ["Motion", "leverarm"], "label": "Vehicle lever arm (m)", "type": "list3",
     "help": "Motion.leverarm, IMU to the vehicle reference point."},
    {"group": "Lever arms", "path": ["GNSS", "leverarm"], "label": "GNSS antenna lever arm (m)", "type": "list3",
     "help": "GNSS.leverarm, IMU to the antenna."},
    {"group": "Lever arms", "path": ["Odometry", "leverarm"], "label": "Camera lever arm (m)", "type": "list3",
     "help": "Odometry.leverarm, IMU to the camera."},
]

KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][\w.-]*)\s*:(.*)$")
TOPIC = re.compile(r"^/[A-Za-z0-9_/~{}]*$")


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------- reading
def params_of(doc: Any) -> Tuple[Optional[str], Optional[Dict]]:
    """(node name, ros__parameters) of a ROS 2 params document."""
    if isinstance(doc, dict):
        for node, section in doc.items():
            if isinstance(section, dict) and isinstance(section.get("ros__parameters"), dict):
                return node, section["ros__parameters"]
    return None, None


def get_path(params: Dict, path: List[str]) -> Tuple[bool, Any]:
    cur: Any = params
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def form_values(text: str) -> Dict[str, Any]:
    _, params = params_of(yaml.safe_load(text) or {})
    out = {}
    for f in FIELDS:
        found, value = get_path(params or {}, f["path"])
        out[".".join(f["path"])] = value if found else None
    return out


# ---------------------------------------------------------------------------- editing
def _yaml_scalar(value: Any, kind: str) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("int", "choice"):
        return str(int(value))
    if kind == "float":
        v = float(value)
        return repr(v) if v != int(v) or abs(v) >= 1e16 else f"{int(v)}.0"
    if kind == "list3":
        return "[" + ", ".join(repr(float(x)) for x in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)          # a quoted string


def _split_comment(rest: str) -> Tuple[str, str]:
    """Split the text after 'key:' into value and trailing comment (a # outside quotes)."""
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i], rest[i:]
    return rest, ""


def set_value(text: str, path: List[str], new_value_yaml: str) -> str:
    """Replace the value of one key (path below the node's ros__parameters), keeping everything else."""
    lines = text.splitlines(keepends=True)
    stack: List[Tuple[int, str]] = []
    want = None
    for i, line in enumerate(lines):
        m = KEY_LINE.match(line.rstrip("\n"))
        if not m or line.lstrip().startswith("#"):
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        keys = [k for _, k in stack] + [key]
        if want is None and len(keys) >= 2 and keys[1] == "ros__parameters":
            want = keys[:2] + list(path)
        if want is not None and keys == want:
            value, comment = _split_comment(rest)
            if not value.strip():
                raise ConfigError(f"{'.'.join(path)} is a section, not a value")
            gap = (value[len(value.rstrip()):] or " ") if comment else ""     # spacing before the comment
            nl = "\n" if line.endswith("\n") else ""
            lines[i] = f"{m.group(1)}{key}: {new_value_yaml}{gap}{comment}{nl}"
            return "".join(lines)
        stack.append((indent, key))
    raise ConfigError(f"{'.'.join(path)} is not in this file; add it in the raw editor")


def apply_form(text: str, changes: Dict[str, Any]) -> str:
    by = {".".join(f["path"]): f for f in FIELDS}
    for key, value in changes.items():
        f = by.get(key)
        if f is None:
            raise ConfigError(f"unknown setting {key}")
        value = check_field(f, value)
        text = set_value(text, f["path"], _yaml_scalar(value, f["type"]))
    return text


def check_field(f: Dict, value: Any) -> Any:
    kind, label = f["type"], f["label"]
    try:
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError
        elif kind in ("int", "choice"):
            value = int(value)
            if kind == "choice" and value not in [c[0] for c in f["choices"]]:
                raise ValueError
        elif kind == "float":
            value = float(value)
        elif kind == "list3":
            value = [float(x) for x in value]
            if len(value) != 3:
                raise ValueError
        elif kind in ("str", "topic", "hostport"):
            value = str(value).strip()
        if kind in ("int", "float") and ("min" in f and value < f["min"] or "max" in f and value > f["max"]):
            raise ConfigError(f"{label}: must be between {f.get('min')} and {f.get('max')}")
        if kind == "topic" and value and not TOPIC.match(value):
            raise ConfigError(f"{label}: a topic starts with / (e.g. /openrtk330/imu/data_raw)")
        if kind == "hostport" and value and not re.match(r"^[A-Za-z0-9.-]+:\d{1,5}$", value):
            raise ConfigError(f"{label}: host:port, e.g. 192.168.116.154:9000")
    except ConfigError:
        raise
    except (TypeError, ValueError):
        raise ConfigError(f"{label}: not a valid value")
    return value


def validate(text: str) -> Tuple[List[str], List[str]]:
    """(errors, warnings) for a whole file."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return [f"not valid YAML{where}: {getattr(e, 'problem', None) or e}"], []
    node, params = params_of(doc)
    if params is None:
        return ["no '<node>: ros__parameters:' section; AIO NAV would start without settings"], []
    errors, warnings = [], []
    for f in FIELDS:
        found, value = get_path(params, f["path"])
        if not found:
            continue
        try:
            check_field(f, value)
        except ConfigError as e:
            errors.append(str(e))
        else:
            if f["type"] == "bool" and not isinstance(value, bool):
                errors.append(f"{f['label']}: must be true or false")
    for key in ("output_rate", "ros_domain_id", "use_sim_time"):
        if key not in params:
            warnings.append(f"{key} is missing; AIO NAV uses its built-in default")
    if params.get("use_sim_time") is True and params.get("ros_localhost_only") == 1:
        warnings.append("use_sim_time is on (bag replay) but ROS is limited to this computer")
    return errors, warnings


def diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"{name} (on disk)", f"{name} (new)", n=2))


# ---------------------------------------------------------------------------- files
def files(cfg) -> List[Dict[str, Any]]:
    """The editable files: one per configured mode that has one."""
    out, active = [], cfg.ros_mode()
    for name, m in cfg["ros"]["modes"].items():
        path = cfg.mode_config_path(name)
        if path:
            st = os.stat(path)
            out.append({"mode": name, "label": m.get("label", name), "name": os.path.basename(path),
                        "path": path, "mtime_ns": st.st_mtime_ns, "active": name == active})
    return out


def file_for(cfg, mode: str) -> Dict[str, Any]:
    for f in files(cfg):
        if f["mode"] == mode:
            return f
    raise ConfigError(f"no config file for mode {mode!r}")


def backup_dir(cfg) -> Path:
    return cfg.state_file("aio_nav_backups")


def backups(cfg, name: str) -> List[Dict[str, Any]]:
    d = backup_dir(cfg)
    if not d.is_dir():
        return []
    items = sorted(d.glob(f"{name}.*.bak"), reverse=True)
    return [{"id": p.name, "time": p.stat().st_mtime, "size": p.stat().st_size} for p in items]


def save(cfg, mode: str, text: str, base_mtime_ns: Optional[int]) -> Dict[str, Any]:
    f = file_for(cfg, mode)
    if base_mtime_ns is not None and int(base_mtime_ns) != f["mtime_ns"]:
        raise ConfigError("the file changed on disk since you opened it; reload and edit again")
    errors, warnings = validate(text)
    if errors:
        raise ConfigError("; ".join(errors))
    old = Path(f["path"]).read_text(encoding="utf-8")
    if old == text:
        return {"changed": False, "warnings": warnings, "diff": ""}
    d = backup_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (d / f"{f['name']}.{stamp}.bak").write_text(old, encoding="utf-8")
    for extra in backups(cfg, f["name"])[MAX_BACKUPS:]:
        (d / extra["id"]).unlink(missing_ok=True)
    path = Path(f["path"])
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)
    cfg.refresh_aio_nav()
    return {"changed": True, "warnings": warnings, "diff": diff(old, text, f["name"]),
            "backup": f"{f['name']}.{stamp}.bak"}


def read_backup(cfg, name: str, backup_id: str) -> str:
    if not re.fullmatch(re.escape(name) + r"\.\d{8}-\d{6}\.bak", backup_id or ""):
        raise ConfigError("unknown backup")
    p = backup_dir(cfg) / backup_id
    if not p.is_file():
        raise ConfigError("unknown backup")
    return p.read_text(encoding="utf-8")
