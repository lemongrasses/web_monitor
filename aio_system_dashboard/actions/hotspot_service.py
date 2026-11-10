"""The system hotspot service (aio_install module "hotspot", unit aio-hotspot.service).

On the RTL8852BE the Wi-Fi can be a hotspot and join a network at the same time, but not through
NetworkManager: hostapd runs the hotspot on a second interface (ap0), started by a root service
that also decides when the Wi-Fi joins a saved network (the hotspot has to stop first, and follows
to the Wi-Fi's channel; on a radar channel it pauses). Where that service is installed the pages do
not start NetworkManager's own either/or hotspot: they show the service and ask it.

* Reading needs no rights: /run/aio-hotspot/status.json (rewritten every few seconds) and
  /etc/aio-hotspot/config.env (name, password, address; readable by the dashboard's user).
* Changing goes through ``sudo -n aio-hotspot <command>``; the module's installer allows exactly
  these: on, off, pause, resume, hold, rescan, set-credentials. The password is passed on standard
  input, never on a command line (sudo logs those).
"""

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

STATUS_FILE = Path("/run/aio-hotspot/status.json")
CONFIG_FILE = Path("/etc/aio-hotspot/config.env")
PROGRAM = "/usr/local/bin/aio-hotspot"
STALE_S = 20.0                      # the service rewrites its status every 3 s
DEFAULT_ADDRESS = "192.168.77.1/24"


def _sudo_runner(argv, stdin: Optional[str], timeout: float) -> Tuple[int, str]:
    try:
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class HotspotService:
    def __init__(self, status_file: Path = STATUS_FILE, config_file: Path = CONFIG_FILE, program: str = PROGRAM,
                 runner: Callable = _sudo_runner, clock: Callable = time.time):
        self.status_file, self.config_file, self.program = Path(status_file), Path(config_file), program
        self._run, self._clock = runner, clock

    def installed(self) -> bool:
        return self.config_file.exists() and Path(self.program).exists()

    def config(self) -> Dict[str, str]:
        """KEY="value" lines of the service's config; {} when it cannot be read."""
        try:
            text = self.config_file.read_text(encoding="utf-8")
        except OSError:
            return {}
        out = {}
        for m in re.finditer(r'(?m)^([A-Z][A-Z0-9_]*)=(?:"([^"]*)"|(\S*))', text):
            out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
        return out

    def status(self) -> Optional[Dict]:
        """What the running service last wrote; None when it is not running."""
        try:
            st = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(st, dict) or self._clock() - float(st.get("updated") or 0) > STALE_S:
            return None
        return st

    def address(self) -> str:
        return self.config().get("HOTSPOT_ADDRESS") or DEFAULT_ADDRESS

    def interface(self) -> str:
        return self.config().get("AP_INTERFACE") or "ap0"

    def password(self) -> str:
        return self.config().get("HOTSPOT_PASSWORD", "")

    def control(self, *args: str, stdin: Optional[str] = None, timeout: float = 30.0) -> Tuple[int, str]:
        return self._run(["sudo", "-n", self.program, *args], stdin, timeout)

    def view(self, port: int) -> Dict:
        """The hotspot as the pages show it (never the password)."""
        st = self.status()
        cfg = self.config()
        h = (st or {}).get("hotspot") or {}
        active = bool(h.get("active"))
        return {"service": True, "running": st is not None, "active": active,
                "saved": True, "autostart": st is not None,
                "ssid": cfg.get("HOTSPOT_SSID") or h.get("ssid") or "",
                "address": self.address().split("/")[0], "port": port,
                "band": h.get("band") or "", "channel": h.get("channel"),
                "clients": int(h.get("clients") or 0) if active else 0,
                "access": h.get("access") or "",
                "reason": (h.get("reason") or "") if st is not None else "switched off",
                "auto_join": bool(((st or {}).get("wifi") or {}).get("auto_join", True))}


SERVICE = HotspotService()          # the one on this computer (tests replace it)


def in_use(cfg, service: Optional[HotspotService] = None) -> bool:
    """Does this device's hotspot come from the service? wifi.hotspot.service: true / false, or
    "auto": when the service is installed."""
    try:
        v = ((cfg["wifi"] or {}).get("hotspot") or {}).get("service", "auto")
    except KeyError:
        v = "auto"
    if v == "auto":
        return (service or SERVICE).installed()
    return v is True
