"""Edit the aio-nav-ros config files (aio_nav.yaml, aio_nav_bag.yaml) from the Maintenance page.

* Only the config files of the configured modes can be opened; the page names a mode, never a path.
* The form changes values in place, line by line, so comments and layout stay as they are. The raw
  editor saves the text as typed. Either way the result must parse, keep `ros__parameters`, and
  pass the checks below before it is written.
* Every save keeps the previous version in state/aio_nav_backups/ (the last 30 per file) and
  refuses to overwrite a file that changed on disk since it was opened. "Changed" is decided by a
  hash of the content (``version``, a string): a nanosecond mtime does not survive a browser,
  whose numbers are exact only up to 2**53, so comparing it gave false conflicts.
* The new text is written to a temporary file in the same folder, flushed to disk, renamed over
  the original (atomic: a reader sees the old or the new file, never half of one), then read back
  and compared before the save is reported as done.
* AIO NAV is stricter than YAML. Starting it on 33 broken copies of aio_nav.yaml (2026-10-21)
  showed it stops at once when a setting is removed or left empty, holds another kind of value
  (text for a number, 1 for true, a list for a text), a list changes length or mixes 3 and 2.5,
  or a decimal setting is written as a whole number (path_publish_rate_hz: 10). So a new file is
  also compared with the one it replaces (compare), and before saving AIO NAV itself is started on
  it for a moment, cut off from every network (test_start): a file it refuses is not saved.
"""

import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

MAX_BACKUPS = 30
BACKUP_ID = r"\.\d{8}-\d{6}(?:-\d+)?\.bak"      # name.YYYYMMDD-HHMMSS[-n].bak
_SAVE_LOCK = threading.Lock()                  # one save at a time (two browsers, one file)

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
     "help": "Motion.leverarm, IMU to the centre of the rear axle (the vehicle reference point)."},
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


def load_yaml(text: str) -> Tuple[Any, Optional[str]]:
    """(document, None) or (None, why). Never raises: very deep nesting (RecursionError) or text
    that is not proper UTF-8 is an error message, not a crash of the request."""
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return None, f"not valid YAML{where}: {getattr(e, 'problem', None) or e}"
    except RecursionError:
        return None, "not usable: nested too deeply"
    except (ValueError, TypeError) as e:
        return None, f"not usable text: {e}"


def form_values(text: str) -> Dict[str, Any]:
    doc, _ = load_yaml(text)
    _, params = params_of(doc or {})
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
    doc, problem = load_yaml(text)
    if problem:
        return [problem], []
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
    errors += _mixed_lists(params, [])
    for key in ("output_rate", "ros_domain_id", "use_sim_time"):
        if key not in params:
            warnings.append(f"{key} is missing; AIO NAV uses its built-in default")
    if params.get("use_sim_time") is True and params.get("ros_localhost_only") == 1:
        warnings.append("use_sim_time is on (bag replay) but ROS is limited to this computer")
    log = params.get("fusion_txt_path")
    if (params.get("save_fusion_txt") is True or params.get("save_parsed_txt") is True) \
            and isinstance(log, str) and os.path.isabs(log):
        folder = os.path.dirname(log)
        if not os.path.isdir(folder):
            warnings.append(f"the log folder {folder} does not exist: AIO NAV runs but writes no logs")
        elif not os.access(folder, os.W_OK | os.X_OK):
            warnings.append(f"the log folder {folder} cannot be written: AIO NAV runs but writes no logs")
    return errors, warnings


# ---------------------------------------------------------------------------- against the file it replaces
MAX_MESSAGES = 8


def _kind(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "int" if isinstance(v, int) else "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "section"
    return "none" if v is None else "other"


def _numeric_text(v: str) -> bool:
    """1e7 is text to PyYAML (YAML 1.1 wants a dot) but a number to ROS and to AIO NAV."""
    try:
        float(v)
        return True
    except ValueError:
        return False


def _cap(items: List[str]) -> List[str]:
    return items if len(items) <= MAX_MESSAGES else items[:MAX_MESSAGES] + [f"and {len(items) - MAX_MESSAGES} more"]


def _mixed_lists(node: Any, path: List[str], depth: int = 0) -> List[str]:
    """A list mixing whole numbers and decimals ([3, 3, 2.5]) stops the ROS parameter parser:
    AIO NAV aborts before it starts, wherever the list is in the file."""
    out: List[str] = []
    if depth > 20:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            out += _mixed_lists(v, path + [str(k)], depth + 1)
    elif isinstance(node, list):
        kinds = {_kind(x) for x in node}
        if {"int", "float"} <= kinds:
            out.append(f"{'.'.join(path)}: write every value with a decimal point (3.0, not 3) or none: "
                       f"a list that mixes 3 and 2.5 stops AIO NAV")
    return out


def compare(old_text: str, new_text: str) -> Tuple[List[str], List[str]]:
    """(errors, warnings) of a new file against the file it replaces, which AIO NAV started with.
    Only what AIO NAV was seen to refuse is an error; a new key is a warning (AIO NAV ignores
    keys it does not know, so it is most likely a misspelling). Whole number -> decimal is left to
    test_start: some settings take it (fine_heading_std_threshold), others stop AIO NAV
    (Alignment.pos.option)."""
    old, _ = load_yaml(old_text)
    new, _ = load_yaml(new_text)
    old_node, old_p = params_of(old)
    new_node, new_p = params_of(new)
    if old_p is None or new_p is None:
        return [], []                   # validate() says what is wrong with the new file
    errors: List[str] = []
    warnings: List[str] = []
    if old_node != new_node:
        errors.append(f"the first line changed from {old_node}: to {new_node}: AIO NAV would not find its settings")
    _compare_section(old_p, new_p, [], errors, warnings)
    return _cap(errors), _cap(warnings)


def _compare_section(old: Dict, new: Dict, path: List[str], errors: List[str], warnings: List[str]) -> None:
    for key, was in old.items():
        name = ".".join(path + [str(key)])
        if key not in new:
            errors.append(f"{name} was removed: AIO NAV does not start without it")
        else:
            _compare_value(name, was, new[key], path + [str(key)], errors, warnings)
    for key in new:
        if key not in old:
            warnings.append(f"{'.'.join(path + [str(key)])} is new: AIO NAV ignores settings it does not know "
                            f"(misspelt?)")


def _compare_value(name: str, was: Any, now: Any, path: List[str], errors: List[str], warnings: List[str]) -> None:
    a, b = _kind(was), _kind(now)
    if "section" in (a, b):
        if a != b:
            errors.append(f"{name} must stay {'a section with settings below it' if a == 'section' else 'a single value'}")
        else:
            _compare_section(was, now, path, errors, warnings)
        return
    if a == "none":
        return                                       # empty before: nothing to compare with
    if b == "none":
        errors.append(f"{name} has no value: AIO NAV does not start with an empty setting")
        return
    if "list" in (a, b):
        if a != b:
            errors.append(f"{name} must stay a list of {len(was)} values" if a == "list" else
                          f"{name} must stay a single value, not a list")
        elif len(now) != len(was):
            errors.append(f"{name} must have {len(was)} values (it has {len(now)})")
        elif all(_kind(x) in ("int", "float") for x in was) and not all(
                _kind(x) in ("int", "float") or (_kind(x) == "str" and _numeric_text(x)) for x in now):
            errors.append(f"{name}: every value must be a number")
        return
    if a == "bool":
        if b != "bool" and not (b == "str" and now.strip().lower() in ("true", "false")):
            errors.append(f"{name} must be true or false")
    elif a in ("int", "float"):
        if b in ("bool", "other") or (b == "str" and not _numeric_text(now)):
            errors.append(f"{name} must be a number")
        elif a == "float" and b == "int":
            errors.append(f"{name}: write {now}.0, not {now}: a decimal setting written as a whole number "
                          f"can stop AIO NAV")
    elif a == "str" and b != "str":
        errors.append(f"{name} must be text: put it in quotes (\"{now}\")")


# ---------------------------------------------------------------------------- test start
TEST_START_S = 8.0          # longest wait; AIO NAV said "ready" after 0.8 s on the Orin NX
TEST_START_SETTLE_S = 1.0   # still running this long after "ready": started
_READY = re.compile(r"\bready\b")
_SAID = re.compile(r"\[(FATAL|ERROR|WARN)\]\s*\[[^\]]*\]\s*(?:\[[^\]]*\]:\s*)?(.*)")
_EXEC_MARK = "aio-nav-test: starting"
_TEST_LOCK = threading.Lock()                  # one test start at a time
_TESTED: Dict[str, Dict[str, Any]] = {}        # content version -> result (the file on disk, re-checked rarely)
# The script runs in a new user + network namespace (unshare -rn, no root needed): only a loopback
# that nothing else shares, so the test cannot reach the drivers' ROS domain, the dashboard's NAV
# port or the vehicle (output_udp is emptied as well). Its logs go to a temporary folder.
_TEST_SCRIPT = ('ip link set lo up || exit 97; ulimit -c 0; . "$1" || exit 98; aio_nav_source_ros >/dev/null 2>&1 '
                '|| exit 98; echo "' + _EXEC_MARK + '"; exec "$2" --ros-args --params-file "$3" -p "config_file:=$3"')
_HINTS = [("Failed to parse global arguments",
           "the file cannot be read as ROS parameters (a list that mixes 3 and 2.5, or a setting left empty?)"),
          ("setting it to {integer} is not allowed", "write the value with a decimal point (10.0, not 10)")]


def node_program(config_path: str) -> Optional[Path]:
    """aio_nav_node of the install the config file belongs to:
    <prefix>/share/aio_nav_ros/config/aio_nav.yaml -> <prefix>/lib/aio_nav_ros/aio_nav_node."""
    p = Path(config_path).resolve()
    if len(p.parents) < 4:
        return None
    node = p.parents[3] / "lib" / "aio_nav_ros" / "aio_nav_node"
    return node if os.access(node, os.X_OK) and (node.parent / "aio_nav_env.sh").is_file() else None


def test_start(config_path: str, text: str, wait_s: float = TEST_START_S) -> Dict[str, Any]:
    """Start AIO NAV on ``text`` for a moment, isolated. {"ran", "ok", "detail", "said"}: ran False
    when it could not be tried here (no program, no namespaces); ok False when AIO NAV stopped."""
    version = version_of(text.encode("utf-8"))
    with _TEST_LOCK:
        if version in _TESTED:
            return _TESTED[version]
        result = _test_start(config_path, text, wait_s)
        if result["ran"]:
            if len(_TESTED) >= 16:
                _TESTED.pop(next(iter(_TESTED)))
            _TESTED[version] = result
        return result


def _test_start(config_path: str, text: str, wait_s: float) -> Dict[str, Any]:
    def no(why: str) -> Dict[str, Any]:
        return {"ran": False, "ok": None, "detail": why, "said": []}

    node = node_program(config_path)
    unshare = shutil.which("unshare")
    if node is None:
        return no("the AIO NAV program was not found next to the config file")
    if unshare is None:
        return no("unshare is not installed")
    with tempfile.TemporaryDirectory(prefix="aio-nav-test-") as tmp:
        copy = text
        for key, value in (("fusion_txt_path", json.dumps(os.path.join(tmp, "test.txt"))), ("output_udp", '""')):
            try:
                copy = set_value(copy, [key], value)
            except ConfigError:
                pass
        cfg_file = os.path.join(tmp, os.path.basename(config_path))
        with open(cfg_file, "w", encoding="utf-8") as fh:
            fh.write(copy)
        program = os.path.join(tmp, "aio_nav_test_node")   # another name: not taken for the running AIO NAV
        os.symlink(node, program)
        env = {"HOME": tmp, "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "LANG": "C.UTF-8", "ROS_LOG_DIR": tmp, "ROS_DOMAIN_ID": "0", "ROS_LOCALHOST_ONLY": "1",
               "RCUTILS_COLORIZED_OUTPUT": "0"}
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen([unshare, "-rn", "bash", "-c", _TEST_SCRIPT, "aio-nav-test",
                                     str(node.parent / "aio_nav_env.sh"), program, cfg_file],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    env=env, cwd=tmp, start_new_session=True)
        except OSError as e:
            return no(f"cannot run unshare: {e}")
        lines: List[str] = []
        reader = threading.Thread(target=lambda: lines.extend(l.decode("utf-8", "replace").rstrip()
                                                              for l in proc.stdout), daemon=True)
        reader.start()
        ready_at = None
        try:
            while True:
                now = time.monotonic()
                if ready_at is None and any(_READY.search(l) for l in lines):
                    ready_at = now
                if proc.poll() is not None:
                    break
                if (ready_at is not None and now - ready_at >= TEST_START_SETTLE_S) or now - t0 >= wait_s:
                    break
                time.sleep(0.05)
        finally:
            running, ended = proc.poll() is None, time.monotonic()
            _stop_group(proc)
            reader.join(2.0)
        took = (ready_at or ended) - t0
    said = [m.group(2) for m in (_SAID.search(l) for l in lines) if m]
    if not running and _EXEC_MARK not in lines:
        if proc.returncode == 97 or any("unshare" in l for l in lines):
            return no("this system does not allow an isolated test start (" + (lines[-1] if lines else "unshare failed") + ")")
        return no("the ROS environment of AIO NAV could not be loaded")
    if running:
        how = f"it reported ready after {took:.1f} s" if ready_at is not None else f"it was still running after {took:.0f} s"
        return {"ran": True, "ok": True, "detail": how, "said": [f"AIO NAV said: {s}" for s in said[:3]]}
    why = next((s for s in said if s), "") or next((l for l in reversed(lines) if l.strip()), "") \
        or f"it stopped at once (exit code {proc.returncode})"
    hint = next((h for pattern, h in _HINTS if pattern in why), "")
    return {"ran": True, "ok": False, "detail": why + (f" ({hint})" if hint else ""), "said": said[:5]}


def _stop_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(wait)
        except subprocess.TimeoutExpired:
            continue


def check_against(config_path: str, old_text: str, new_text: str, test: bool) -> Tuple[List[str], List[str]]:
    """compare(), when the file on disk is one AIO NAV starts with. A file broken by hand must not
    hold its mistakes in place (restoring a backup would be refused), so if it does not start
    either, the new file is judged by its own test start only."""
    errors, warnings = compare(old_text, new_text)
    if errors and test:
        res = test_start(config_path, old_text)
        if res["ran"] and not res["ok"]:
            return [], warnings + [f"the file on disk does not start AIO NAV either ({res['detail']}), so the "
                                   f"new one is only checked by starting AIO NAV on it when it is saved"]
    return errors, warnings


def diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"{name} (on disk)", f"{name} (new)", n=2))


# ---------------------------------------------------------------------------- files
def version_of(data: bytes) -> str:
    """Opaque version of a file's content. A string, so it reaches the browser and comes back
    unchanged (a nanosecond mtime is ~1.8e18, beyond the 2**53 a JavaScript number keeps exact)."""
    return hashlib.sha256(data).hexdigest()


def files(cfg) -> List[Dict[str, Any]]:
    """The editable files: one per configured mode that has one."""
    out, active = [], cfg.ros_mode()
    for name, m in cfg["ros"]["modes"].items():
        path = cfg.mode_config_path(name)
        if path:
            with open(path, "rb") as fh:
                version = version_of(fh.read())
            out.append({"mode": name, "label": m.get("label", name), "name": os.path.basename(path),
                        "path": path, "version": version, "active": name == active,
                        "writable": _writable(path)})
    return out


def _writable(path: str) -> bool:
    """Saving replaces the file, so its folder must be writable, not only the file itself."""
    folder = os.path.dirname(os.path.abspath(path))
    return os.access(path, os.W_OK) and os.access(folder, os.W_OK | os.X_OK)


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
    pattern = re.compile(re.escape(name) + BACKUP_ID)
    items = []
    for p in d.glob(f"{name}.*.bak"):
        if pattern.fullmatch(p.name):
            st = p.stat()
            items.append((st.st_mtime_ns, p.name, st))
    # Newest first by write time: names alone misorder "x.<stamp>-1.bak" before "x.<stamp>.bak".
    items.sort(key=lambda i: (i[0], i[1]), reverse=True)
    return [{"id": n, "time": st.st_mtime, "size": st.st_size} for _, n, st in items]


class ConflictError(ConfigError):
    """The file changed on disk since the editor opened it."""


def test_start_enabled(cfg) -> bool:
    return not cfg.fake and bool(cfg["maintenance"].get("config_test_start", True))


def save(cfg, mode: str, text: str, base_version: Optional[str]) -> Dict[str, Any]:
    """Validate and write ``text``. base_version: the ``version`` the editor opened (None: no
    check). Raises ConflictError if the file changed since, ConfigError for bad text, a file AIO
    NAV does not start with, or a folder that cannot be written; an OSError while writing leaves
    the original file intact."""
    errors, warnings = validate(text)
    if errors:
        raise ConfigError("; ".join(errors))
    test = test_start_enabled(cfg)
    with _SAVE_LOCK:
        f = file_for(cfg, mode)
        path = Path(f["path"])
        old_bytes = path.read_bytes()
        current = version_of(old_bytes)
        if base_version is not None and str(base_version) != current:
            raise ConflictError("the file changed on disk since you opened it; reload and edit again")
        old = old_bytes.decode("utf-8")
        if old == text:
            return {"changed": False, "warnings": warnings, "diff": "", "version": current}
        errors, more = check_against(f["path"], old, text, test)
        if errors:
            raise ConfigError("; ".join(errors))
        warnings += more
        if not f["writable"]:
            raise ConfigError(f"cannot write {path}: this account may not replace files in "
                              f"{path.parent} (the folder must be writable, not only the file)")
        started = test_start(f["path"], text) if test else \
            {"ran": False, "ok": None, "detail": "test start is off", "said": []}
        if started["ran"] and not started["ok"]:
            raise ConfigError(f"not saved: AIO NAV does not start with this file: {started['detail']}")
        if test and not started["ran"]:
            warnings.append(f"AIO NAV could not be test-started here ({started['detail']}); "
                            f"the file was only checked by the rules above")
        warnings += started["said"]
        backup = _write_backup(cfg, f["name"], old)
        new_version = _atomic_write(path, text.encode("utf-8"))
    cfg.refresh_aio_nav()
    return {"changed": True, "warnings": warnings, "diff": diff(old, text, f["name"]),
            "backup": backup, "version": new_version, "test_start": started}


def _write_backup(cfg, name: str, old: str) -> str:
    d = backup_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bid, n = f"{name}.{stamp}.bak", 1
    while (d / bid).exists():                   # two saves in the same second keep both versions
        bid, n = f"{name}.{stamp}-{n}.bak", n + 1
    (d / bid).write_text(old, encoding="utf-8")
    for extra in backups(cfg, name)[MAX_BACKUPS:]:
        (d / extra["id"]).unlink(missing_ok=True)
    return bid


def _atomic_write(path: Path, data: bytes) -> str:
    """Write next to the file, fsync, rename over it, fsync the folder, read back. Returns the new
    version. On any failure the temporary file is removed and the original stays as it was."""
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:                                        # make the rename itself survive a power cut
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    written = path.read_bytes()
    if written != data:
        raise OSError(f"{path} reads back different from what was written")
    return version_of(written)


def read_backup(cfg, name: str, backup_id: str) -> str:
    if not re.fullmatch(re.escape(name) + BACKUP_ID, backup_id or ""):
        raise ConfigError("unknown backup")
    p = backup_dir(cfg) / backup_id
    if not p.is_file():
        raise ConfigError("unknown backup")
    return p.read_text(encoding="utf-8")
