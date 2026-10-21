"""Copies of the vehicle's UDP packets for the devices on the Wi-Fi hotspot.

AIO NAV sends its output to one outside address (output_udp: the vehicle, on the cable) and to the
dashboard (127.0.0.1:9000). A laptop or tablet on the hotspot that listens on UDP gets a copy from
the dashboard: every NAV packet the dashboard receives and every 0x05 status packet it sends, to
``wifi.hotspot.relay_port`` of each device. Unicast to each device, not a broadcast: Wi-Fi retries a
unicast frame, and sends a broadcast once at its slowest rate.

The devices are found by themselves every REFRESH_S: the addresses in the hotspot's network in the
Wi-Fi's neighbour table (ip neigh) whose MAC is associated now (asked from the kernel, nl80211.py: no
iw package needed); one that left is dropped. Only while the Wi-Fi is the hotspot. Switched on Maintenance > Network (kept in
state/hotspot_relay.json; until then wifi.hotspot.relay_udp).
"""

import ipaddress
import json
import logging
import re
import socket
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .. import nl80211

logger = logging.getLogger(__name__)

REFRESH_S = 2.0
STATE_FILE = "hotspot_relay.json"


def _run(argv: List[str], timeout: float = 3.0) -> Tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout


def neighbours(ip_neigh: str) -> List[Tuple[str, str]]:
    """(address, MAC) of the usable entries of `ip -4 neigh show dev X`."""
    out = []
    for line in ip_neigh.splitlines():
        m = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+lladdr\s+([0-9a-fA-F:]{17})\s+(\S+)", line.strip())
        if m and m.group(3) not in ("FAILED", "INCOMPLETE"):
            out.append((m.group(1), m.group(2).lower()))
    return out


class HotspotRelay:
    name = "hotspot-relay"

    def __init__(self, cfg, state_file, events=None, runner: Callable = _run, offered: Callable = None,
                 access_point: Callable = nl80211.access_point):
        h = (cfg.data.get("wifi") or {}).get("hotspot") or {}
        try:
            self.network = ipaddress.ip_interface(str(h.get("address") or "10.42.0.1/24")).network
        except ValueError:
            self.network = ipaddress.ip_network("10.42.0.0/24")
        self.port = int(h.get("relay_port", 9000))
        self.default = h.get("relay_udp", True) is not False
        self._file = state_file
        self.events = events
        self._run = runner
        self._offered = offered or (lambda: True)
        self._access_point = access_point
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self.enabled = self._load()
        self.active = False                       # the Wi-Fi is the hotspot
        self.targets: List[Tuple[str, int]] = []
        self.sent = 0
        self.errors = 0
        self.last_error = ""

    # ------------------------------------------------------------------ switch
    def _load(self) -> bool:
        try:
            return bool(json.loads(self._file.read_text(encoding="utf-8"))["enabled"])
        except (OSError, ValueError, KeyError, TypeError):
            return self.default

    def set_enabled(self, enabled: bool, who: str = "") -> None:
        self.enabled = bool(enabled)
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"enabled": self.enabled, "changed_at": time.time(), "changed_by": who}),
                           encoding="utf-8")
            tmp.replace(self._file)
        except OSError as e:
            logger.warning("cannot keep the hotspot relay setting: %s", e)
        if self.events:
            self.events.add("info", "network", "UDP copies to hotspot devices " + ("on" if enabled else "off"), who)

    # ------------------------------------------------------------------ devices
    def refresh(self) -> None:
        targets: List[Tuple[str, int]] = []
        active = False
        if self._offered():
            try:
                ap = self._access_point()
            except OSError as e:                      # said once, not every REFRESH_S
                if self.last_error != f"cannot read the Wi-Fi: {e}":
                    self.last_error = f"cannot read the Wi-Fi: {e}"
                    logger.warning("hotspot relay: %s", self.last_error)
                ap = None
            if ap:
                dev, macs = ap
                active = True
                rc, neigh = self._run(["ip", "-4", "neigh", "show", "dev", dev])
                for addr, mac in (neighbours(neigh) if rc == 0 else []):
                    if mac in macs and ipaddress.ip_address(addr) in self.network:
                        targets.append((addr, self.port))
        with self._lock:
            self.active, self.targets = active, sorted(set(targets))

    # ------------------------------------------------------------------ copies
    def send(self, data: bytes) -> None:
        """A copy to each device on the hotspot (the NAV thread and the status sender call this)."""
        if not self.enabled:
            return
        with self._lock:
            targets = list(self.targets)
        if not targets:
            return
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setblocking(False)
            for t in targets:
                try:
                    self._sock.sendto(data, t)
                    self.sent += 1
                except OSError as e:                  # a device that just left, a full buffer
                    self.errors += 1
                    self.last_error = f"{t[0]}: {e.strerror or e}"
        except OSError as e:
            self.errors += 1
            self.last_error = str(e)

    def status(self) -> Dict:
        with self._lock:
            targets = [a for a, _ in self.targets]
        return {"enabled": self.enabled, "active": self.active, "port": self.port, "targets": targets,
                "sent": self.sent, "errors": self.errors, "last_error": self.last_error}

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:                         # never take the dashboard down
                logger.exception("hotspot relay: refresh failed")
            self._stop.wait(REFRESH_S)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread:
            self._thread.join(timeout)
