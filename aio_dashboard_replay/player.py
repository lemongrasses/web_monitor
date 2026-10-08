"""Play a bag with ``ros2 bag play`` from the Maintenance page.

* Every option of ``ros2 bag play`` (ROS 2 Humble) is described in OPTIONS, so the page can offer
  all of them; values are checked here and turned into an argument list (never a shell string).
* The player runs in the domain of the active mode (Bag replay), in its own session, with output in
  logs/bag_play.log. Only one playback at a time. Its state is kept in state/bag_play.json so a
  dashboard restart does not lose track of it.
* Pause, resume and rate changes go through the player's own services (/rosbag2_player/...).
* The position is an estimate (start offset + elapsed time x rate, minus pauses); the player does
  not report it.
"""

import glob
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOPIC_RE = re.compile(r"^/?[A-Za-z_~{][A-Za-z0-9_/{}~]*$")

# Every option of `ros2 bag play` on ROS 2 Humble. basic: shown first; the rest under "Advanced".
OPTIONS: List[Dict] = [
    {"key": "topics", "flag": "--topics", "kind": "topics", "basic": True, "default": [],
     "label": "Topics", "help": "Topics to play. None ticked: all topics in the bag."},
    {"key": "rate", "flag": "--rate", "kind": "float", "basic": True, "default": 1.0, "min": 0.01, "max": 100,
     "label": "Rate", "help": "Playback speed: 1 = real time, 0.5 = half speed."},
    {"key": "start_offset", "flag": "--start-offset", "kind": "float", "basic": True, "default": 0.0, "min": 0,
     "label": "Start at (s)", "help": "Start this many seconds into the bag."},
    {"key": "loop", "flag": "--loop", "kind": "bool", "basic": True, "default": False,
     "label": "Loop", "help": "Start again from the beginning at the end, until stopped."},
    {"key": "start_paused", "flag": "--start-paused", "kind": "bool", "basic": True, "default": False,
     "label": "Start paused", "help": "Open the bag but wait for Resume."},
    {"key": "clock", "flag": "--clock", "kind": "float", "basic": False, "default": 100.0, "min": 0, "max": 10000,
     "label": "/clock (Hz)", "help": "Publish /clock at this rate, the time source for nodes with use_sim_time "
                                     "(the Bag replay config uses it). 0: do not publish /clock."},
    {"key": "delay", "flag": "--delay", "kind": "float", "basic": False, "default": 0.0, "min": 0,
     "label": "Delay (s)", "help": "Wait this long before playing (each loop)."},
    {"key": "read_ahead_queue_size", "flag": "--read-ahead-queue-size", "kind": "int", "basic": False,
     "default": 1000, "min": 1, "label": "Read-ahead queue",
     "help": "Messages held in memory ahead of playback. Larger: smoother, more memory."},
    {"key": "remap", "flag": "--remap", "kind": "remap", "basic": False, "default": [],
     "label": "Remap topics", "help": "One per line: /old_topic:=/new_topic"},
    {"key": "storage", "flag": "--storage", "kind": "choice", "basic": False, "default": "",
     "choices": ["", "sqlite3"], "label": "Storage plugin", "help": "Empty: detect from the bag."},
    {"key": "qos_profile_overrides_path", "flag": "--qos-profile-overrides-path", "kind": "file",
     "basic": False, "default": "", "label": "QoS overrides file", "help": "YAML file with QoS per topic."},
    {"key": "storage_config_file", "flag": "--storage-config-file", "kind": "file", "basic": False,
     "default": "", "label": "Storage config file", "help": "YAML file with storage plugin settings."},
    {"key": "wait_for_all_acked", "flag": "--wait-for-all-acked", "kind": "int", "basic": False,
     "default": None, "min": 0, "optional": True, "label": "Wait for all acked (ms)",
     "help": "At the end, wait until subscribers acknowledged every message (RELIABLE topics only). "
             "Empty: off. 0: wait forever."},
    {"key": "disable_loan_message", "flag": "--disable-loan-message", "kind": "bool", "basic": False,
     "default": False, "label": "Disable loaned messages", "help": "Publish without loaned messages."},
    {"key": "log_level", "flag": "--log-level", "kind": "choice", "basic": False, "default": "info",
     "choices": ["debug", "info", "warn", "error", "fatal"], "label": "Log level", "help": ""},
]
BY_KEY = {o["key"]: o for o in OPTIONS}


class OptionError(ValueError):
    pass


def defaults() -> Dict:
    return {o["key"]: (list(o["default"]) if isinstance(o["default"], list) else o["default"]) for o in OPTIONS}


def validate(raw: Dict, bag: Dict) -> Dict:
    """Checked option values (unknown keys rejected, numbers range-checked, topics in the bag)."""
    if not isinstance(raw, dict):
        raise OptionError("options must be an object")
    unknown = set(raw) - set(BY_KEY)
    if unknown:
        raise OptionError(f"unknown option(s): {', '.join(sorted(unknown))}")
    out = defaults()
    for key, value in raw.items():
        o = BY_KEY[key]
        kind = o["kind"]
        if kind == "bool":
            out[key] = bool(value)
        elif kind in ("float", "int"):
            if value in (None, "") and o.get("optional"):
                out[key] = None
                continue
            try:
                num = float(value) if kind == "float" else int(value)
            except (TypeError, ValueError):
                raise OptionError(f"{o['label']}: not a number")
            if "min" in o and num < o["min"] or "max" in o and num > o["max"]:
                raise OptionError(f"{o['label']}: must be between {o.get('min', '-')} and {o.get('max', '-')}")
            out[key] = num
        elif kind == "choice":
            if value not in o["choices"]:
                raise OptionError(f"{o['label']}: must be one of {', '.join(c or '(auto)' for c in o['choices'])}")
            out[key] = value
        elif kind == "file":
            value = str(value or "").strip()
            if value and not os.path.isfile(os.path.expanduser(value)):
                raise OptionError(f"{o['label']}: file not found: {value}")
            out[key] = value
        elif kind == "topics":
            names = {t["name"] for t in bag.get("topics", [])}
            topics = [t for t in (value or []) if isinstance(t, str)]
            missing = [t for t in topics if t not in names]
            if missing:
                raise OptionError(f"not in this bag: {', '.join(missing)}")
            out[key] = sorted(set(topics))
        elif kind == "remap":
            lines = value if isinstance(value, list) else str(value or "").splitlines()
            pairs = []
            for line in (l.strip() for l in lines):
                if not line:
                    continue
                src, sep, dst = line.partition(":=")
                if not sep or not TOPIC_RE.match(src.strip()) or not TOPIC_RE.match(dst.strip()):
                    raise OptionError(f"remap: '{line}' is not /old:=/new")
                pairs.append(f"{src.strip()}:={dst.strip()}")
            out[key] = pairs
    if out["start_offset"] and bag.get("duration_s") and out["start_offset"] >= bag["duration_s"]:
        raise OptionError(f"Start at: the bag is only {bag['duration_s']:.0f} s long")
    return out


def play_args(opts: Dict, bag_path: str) -> List[str]:
    """Arguments after `ros2 bag play` (keyboard controls off: there is no terminal)."""
    a: List[str] = []
    if opts["topics"]:
        a += ["--topics", *opts["topics"]]
    if opts["rate"] != 1.0:
        a += ["--rate", _num(opts["rate"])]
    if opts["start_offset"]:
        a += ["--start-offset", _num(opts["start_offset"])]
    if opts["loop"]:
        a.append("--loop")
    if opts["start_paused"]:
        a.append("--start-paused")
    if opts["clock"]:
        a += ["--clock", _num(opts["clock"])]
    if opts["delay"]:
        a += ["--delay", _num(opts["delay"])]
    if opts["read_ahead_queue_size"] != BY_KEY["read_ahead_queue_size"]["default"]:
        a += ["--read-ahead-queue-size", str(int(opts["read_ahead_queue_size"]))]
    if opts["remap"]:
        a += ["--remap", *opts["remap"]]
    if opts["storage"]:
        a += ["--storage", opts["storage"]]
    for key in ("qos_profile_overrides_path", "storage_config_file"):
        if opts[key]:
            a += [BY_KEY[key]["flag"], os.path.expanduser(opts[key])]
    if opts["wait_for_all_acked"] is not None:
        a += ["--wait-for-all-acked", str(int(opts["wait_for_all_acked"]))]
    if opts["disable_loan_message"]:
        a.append("--disable-loan-message")
    if opts["log_level"] != "info":
        a += ["--log-level", opts["log_level"]]
    a.append("--disable-keyboard-controls")
    return a + [bag_path]


def _num(x: float) -> str:
    return ("%f" % x).rstrip("0").rstrip(".")


def find_ros_setup(configured: str, aio_nav_path: str = "") -> Optional[str]:
    """setup.bash to source: the configured one, else the aio-nav-ros install (it knows its own
    message types and sources ROS), else /opt/ros/<distro>."""
    if configured and configured != "auto":
        p = os.path.expanduser(configured)
        return p if os.path.isfile(p) else None
    if aio_nav_path:
        for parent in Path(aio_nav_path).resolve().parents:
            if parent.name == "install" and (parent / "setup.bash").is_file():
                return str(parent / "setup.bash")
    found = sorted(glob.glob("/opt/ros/*/setup.bash"))
    return found[-1] if found else None


def shell_argv(setup: str, ros2_args: List[str]) -> List[str]:
    """bash that sources ROS, then runs ros2 with the arguments as separate words (no quoting)."""
    return ["bash", "-c", 'source "$0" >/dev/null 2>&1; exec ros2 "$@"', setup] + ros2_args


class Progress:
    """Estimated position in the bag: rate x time, frozen while paused, wrapped when looping."""

    def __init__(self, d: Dict):
        self.d = d                     # persisted fields (part of the playback state)

    @classmethod
    def new(cls, opts: Dict, duration: float, now: float) -> "Progress":
        return cls({"pos": float(opts["start_offset"]), "at": now + float(opts["delay"]),
                    "rate": float(opts["rate"]), "paused": bool(opts["start_paused"]),
                    "loop": bool(opts["loop"]), "duration": duration})

    def position(self, now: float) -> float:
        d = self.d
        pos = d["pos"] + (0.0 if d["paused"] else max(0.0, now - d["at"]) * d["rate"])
        dur = d["duration"]
        if dur > 0:
            pos = pos % dur if d["loop"] else min(pos, dur)
        return pos

    def change(self, now: float, paused: Optional[bool] = None, rate: Optional[float] = None) -> None:
        self.d["pos"], self.d["at"] = self.position(now), now
        if paused is not None:
            self.d["paused"] = paused
        if rate is not None:
            self.d["rate"] = rate


def _cmdline(pid: int) -> List[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _is_player(pid: int) -> bool:
    words = _cmdline(pid)
    return "bag" in words and "play" in words


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return st != "Z"
    except (ProcessLookupError, OSError, IndexError):
        return False
    except PermissionError:
        return True


class Player:
    def __init__(self, state_file: Path, log_file: Path, presets_file: Path, events=None,
                 clock=time.time):
        self.state_file, self.log_file, self.presets_file = state_file, log_file, presets_file
        self.events = events
        self.clock = clock
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self.state: Dict = self._load(state_file) or {}

    # ------------------------------------------------------------------ persistence
    @staticmethod
    def _load(path: Path) -> Optional[Dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _save(path: Path, data: Dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _event(self, level: str, title: str, detail: str = "") -> None:
        if self.events is not None:
            self.events.add(level, "replay", title, detail)

    # ------------------------------------------------------------------ status
    def running(self) -> bool:
        pid = self.state.get("pid")
        return bool(pid and self.state.get("status") == "playing" and _alive(pid) and _is_player(pid))

    def status(self) -> Dict:
        with self._lock:
            st = dict(self.state)
            if st.get("status") == "playing" and not self.running():
                # it ended by itself (or the dashboard restarted after it ended)
                code = self._proc.returncode if self._proc is not None else None
                st["status"] = "finished" if code in (0, None) else "failed"
                st["exit_code"] = code
                st["ended"] = st.get("ended") or self.clock()
                if code not in (0, None):
                    st["error"] = self.log_tail(4)
                self.state = st
                self._save(self.state_file, st)
                self._event("info" if st["status"] == "finished" else "warning",
                            f"Bag playback {st['status']}", st.get("bag_name", ""))
            if st.get("progress"):
                now = self.clock() if st.get("status") == "playing" else (st.get("ended") or self.clock())
                st["position_s"] = round(Progress(st["progress"]).position(now), 1)
                st["paused"] = st["progress"]["paused"]
                st["rate_now"] = st["progress"]["rate"]
            return st

    def log_tail(self, n: int = 20) -> str:
        try:
            lines = self.log_file.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-n:])

    # ------------------------------------------------------------------ start / stop
    def start(self, bag: Dict, opts: Dict, setup: str, env: Dict[str, str], cwd: str) -> Dict:
        with self._lock:
            if self.running():
                return {"success": False, "summary": "a bag is already playing; stop it first"}
            argv = shell_argv(setup, ["bag", "play"] + play_args(opts, bag["path"]))
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(self.log_file, "wb") as out:
                    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=subprocess.STDOUT, start_new_session=True,
                                            env=env, cwd=cwd)
            except OSError as e:
                return {"success": False, "summary": f"cannot start ros2 bag play: {e}"}
            self._proc = proc
            threading.Thread(target=proc.wait, daemon=True, name="bag-play-reap").start()
            now = self.clock()
            self.state = {"status": "playing", "pid": proc.pid, "bag_id": bag["id"], "bag_name": bag["name"],
                          "bag_path": bag["path"], "duration_s": bag["duration_s"], "options": opts,
                          "started": now, "domain": env.get("ROS_DOMAIN_ID"),
                          "command": command_text(opts, bag["path"], env),
                          "progress": Progress.new(opts, bag["duration_s"], now).d}
            self._save(self.state_file, self.state)
        time.sleep(1.5)                      # a bad bag or option fails at once
        if proc.poll() is not None and proc.returncode != 0:
            st = self.status()
            return {"success": False, "summary": "ros2 bag play exited: " + (st.get("error") or "see the log")}
        self._event("info", "Bag playback started", f"{bag['name']} ({self.state['command']})")
        return {"success": True, "summary": f"playing {bag['name']}"}

    def stop(self, grace_s: float = 5.0) -> Dict:
        with self._lock:
            pid = self.state.get("pid")
            if not pid or not _alive(pid) or not _is_player(pid):
                if self.state.get("status") == "playing":
                    self.state.update(status="stopped", ended=self.clock())
                    self._save(self.state_file, self.state)
                return {"success": True, "summary": "nothing is playing"}
            for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
                try:
                    os.killpg(pid, sig)          # the whole session: bash exec'd ros2 and its children
                except ProcessLookupError:
                    break
                end = time.monotonic() + wait
                while time.monotonic() < end and _alive(pid):
                    time.sleep(0.1)
                if not _alive(pid):
                    break
            ok = not _alive(pid)
            if self.state.get("progress"):
                Progress(self.state["progress"]).change(self.clock())
            self.state.update(status="stopped" if ok else "playing", ended=self.clock())
            self._save(self.state_file, self.state)
        self._event("info", "Bag playback stopped", self.state.get("bag_name", ""))
        return {"success": ok, "summary": "stopped" if ok else f"could not stop pid {pid}"}

    # ------------------------------------------------------------------ live control
    def control(self, action: str, setup: str, env: Dict[str, str], rate: Optional[float] = None,
                timeout_s: float = 10.0) -> Dict:
        if not self.running():
            return {"success": False, "summary": "nothing is playing"}
        if action == "pause":
            call = ["service", "call", "/rosbag2_player/pause", "rosbag2_interfaces/srv/Pause", "{}"]
        elif action == "resume":
            call = ["service", "call", "/rosbag2_player/resume", "rosbag2_interfaces/srv/Resume", "{}"]
        elif action == "set_rate":
            if rate is None or not (0.01 <= float(rate) <= 100):
                return {"success": False, "summary": "rate must be between 0.01 and 100"}
            call = ["service", "call", "/rosbag2_player/set_rate", "rosbag2_interfaces/srv/SetRate",
                    "{rate: %s}" % _num(float(rate))]
        else:
            return {"success": False, "summary": f"unknown action {action!r}"}
        t0 = self.clock()
        try:
            r = subprocess.run(shell_argv(setup, call), env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"success": False, "summary": "the player did not answer"}
        # ros2 needs about a second to start; the player acted near the end of the call
        when = t0 + 0.8 * (self.clock() - t0)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 or "response:" not in out:
            return {"success": False, "summary": out.splitlines()[-1] if out else "service call failed"}
        if action == "set_rate" and "success=False" in out:
            return {"success": False, "summary": "the player refused the rate"}
        with self._lock:
            prog = Progress(self.state["progress"])
            if action == "pause":
                prog.change(when, paused=True)
            elif action == "resume":
                prog.change(when, paused=False)
            else:
                prog.change(when, rate=float(rate))
            self._save(self.state_file, self.state)
        return {"success": True, "summary": {"pause": "paused", "resume": "playing",
                                             "set_rate": f"rate {_num(float(rate or 0))}x"}[action]}

    # ------------------------------------------------------------------ presets / last choice
    def presets(self) -> Dict:
        data = self._load(self.presets_file) or {}
        return {"presets": data.get("presets") or {}, "last": data.get("last") or {}}

    def save_preset(self, name: str, bag: Dict, opts: Dict) -> Dict:
        name = name.strip()
        if not name or len(name) > 60:
            return {"success": False, "summary": "name: 1-60 characters"}
        data = self.presets()
        data["presets"][name] = {"bag_id": bag["id"], "bag_name": bag["name"], "options": opts}
        self._save(self.presets_file, data)
        return {"success": True, "summary": f"saved '{name}'"}

    def delete_preset(self, name: str) -> Dict:
        data = self.presets()
        if data["presets"].pop(name, None) is None:
            return {"success": False, "summary": f"no preset '{name}'"}
        self._save(self.presets_file, data)
        return {"success": True, "summary": f"deleted '{name}'"}

    def remember(self, bag: Dict, opts: Dict) -> None:
        data = self.presets()
        data["last"][bag["id"]] = opts
        data["last"] = dict(list(data["last"].items())[-50:])
        try:
            self._save(self.presets_file, data)
        except OSError:
            pass


def command_text(opts: Dict, bag_path: str, env: Dict[str, str]) -> str:
    """The same playback as one line to paste in a terminal."""
    pre = " ".join(f"{k}={env[k]}" for k in ("ROS_DOMAIN_ID", "ROS_LOCALHOST_ONLY") if k in env)
    args = [a for a in play_args(opts, bag_path) if a != "--disable-keyboard-controls"]
    return (pre + " " if pre else "") + "ros2 bag play " + " ".join(shlex.quote(a) for a in args)


def process_domain(pid: int) -> Optional[int]:
    """ROS domain a running process uses (from its environment; unset = 0). None: unreadable."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    env = dict(kv.decode(errors="replace").split("=", 1) for kv in raw if b"=" in kv)
    try:
        return int(env.get("ROS_DOMAIN_ID") or 0)
    except ValueError:
        return None


def preconditions(cfg, services: Dict, playing: bool, driver_domains=None) -> Tuple[List[Dict], bool]:
    """What has to be true before playing; each item says how to fix it. (items, all_ok)

    driver_domains(key) -> the ROS domains the driver's processes run in (None: unknown). Running
    drivers only matter when they publish in the bag's domain: different domains never see each
    other, so live and recorded data cannot mix."""
    items = []
    info = cfg.ros_mode_info() if hasattr(cfg, "ros_mode_info") else {"modes": []}
    bag_domain = next((m.get("domain_id") for m in info["modes"] if m["name"] == "bag"), None)
    mode = cfg.ros_mode()
    bag_mode = "bag"
    label = (cfg["ros"]["modes"].get(bag_mode) or {}).get("label", "Bag replay")
    now_label = (cfg["ros"]["modes"].get(mode) or {}).get("label") if mode else None
    items.append({"key": "mode", "ok": mode == bag_mode,
                  "text": f"ROS environment: {label}" if mode == bag_mode else
                          f"ROS environment: {now_label or 'not set'}, not {label}. AIO NAV and the "
                          f"dashboard would not see the bag",
                  "fix": None if mode == bag_mode else {"kind": "mode", "mode": bag_mode,
                                                        "label": f"Switch to {label}"}})
    for key, svc in cfg["services"].items():
        if not svc.get("user_unit"):
            continue
        running = (services.get(key) or {}).get("state") == "running"
        name = svc.get("label", key)
        domains = driver_domains(key) if (running and driver_domains) else None
        if not running:
            ok, text = True, f"{name}: stopped"
        elif domains and bag_domain is not None and bag_domain not in domains:
            ok = True
            text = (f"{name}: running in domain {', '.join(str(d) for d in sorted(domains))}, separate "
                    f"from the bag (domain {bag_domain}), so they do not mix. They still use CPU and "
                    f"camera bandwidth.")
        elif domains:
            ok, text = False, f"{name}: running in the bag's domain {bag_domain}, so live and recorded data would mix"
        else:
            ok, text = False, f"{name}: running, and its ROS domain cannot be read; stop it to be sure the data do not mix"
        items.append({"key": f"driver:{key}", "ok": ok, "text": text,
                      "fix": None if ok else {"kind": "action", "action": "stop_driver",
                                              "target": key, "label": f"Stop {name}"}})
    items.append({"key": "idle", "ok": not playing,
                  "text": "No other playback" if not playing else "A bag is already playing",
                  "fix": None})
    return items, all(i["ok"] for i in items)
