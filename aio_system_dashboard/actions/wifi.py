"""Wi-Fi from the user and Maintenance pages, through NetworkManager (nmcli).

Safety rules:
* Changes are only accepted from a browser whose own connection to this device does not go over
  Wi-Fi (Ethernet, or this computer): changing Wi-Fi can then never cut off the page that made the
  change. See ``client_path``.
* No root while running. ``install.sh --with-wifi`` installs, once, a polkit rule that lets the
  dashboard's user manage NetworkManager. The code only ever creates, deletes or activates Wi-Fi
  (802-11-wireless) profiles; wired profiles are never touched.
* A connection attempt records the active Wi-Fi profile first. If the new network fails (wrong
  password, no association, no IPv4 within ``ip_timeout_s``), the profiles it created are deleted
  and the previous Wi-Fi connection is brought back.
* The password goes to nmcli only. It is never logged, recorded in events, returned by the API, or
  shown in an error: anything nmcli prints is scrubbed of it first. (nmcli takes it on its command
  line, so it is visible in /proc for the second the command runs; the device has no other users.)
* One change at a time.
"""

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WIFI_TYPE = "802-11-wireless"
NO_PERMISSION = ("not authorized", "insufficient privileges", "not allowed", "permission denied")
PERMISSION_HINT = ("this account may not change Wi-Fi; run 'sudo deploy/install.sh --with-wifi' once "
                   "(it allows the dashboard's user to manage Wi-Fi, nothing else needs root)")


def failure_code(text: str) -> str:
    """Why nmcli failed, as a code the pages turn into plain words."""
    low = (text or "").lower()
    if any(w in low for w in NO_PERMISSION):
        return "denied"
    if "secrets were required" in low or "psk" in low or "passphrase" in low or "password" in low:
        return "wrong_password"
    if "no network with ssid" in low or "not found" in low:
        return "not_found"
    if "timed out" in low or "timeout" in low:
        return "timeout"
    return "failed"


class WifiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def split_terse(line: str) -> List[str]:
    """Split one line of `nmcli -t` output: fields are separated by ':', and ':' or '\\' inside a
    field are escaped with a backslash (an SSID may contain both)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def scrub(text: str, secret: Optional[str]) -> str:
    return text.replace(secret, "********") if secret else text


def _nm_runner(argv: List[str], timeout: float) -> Tuple[int, str]:
    """Run nmcli with English messages; (exit code, stdout + stderr)."""
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class WifiManager:
    def __init__(self, cfg, events, runner: Callable[[List[str], float], Tuple[int, str]] = _nm_runner,
                 clock=time.monotonic, sleep=time.sleep):
        self.cfg, self.events = cfg, events
        try:
            w = cfg["wifi"] or {}
        except KeyError:                                  # a config without the section: defaults
            w = {}
        self.enabled = bool(w.get("enabled", True))
        self.interface = w.get("interface") or "auto"
        self.connect_timeout_s = float(w.get("connect_timeout_s", 30))
        self.ip_timeout_s = float(w.get("ip_timeout_s", 30))
        self._run, self._clock, self._sleep = runner, clock, sleep
        self._lock = threading.Lock()
        self.last_result: Optional[Dict] = None
        self._restored, self._kept = "", False
        self.fake = bool(getattr(cfg, "fake", False))
        self._fake_connected = "lab-wifi"

    # ------------------------------------------------------------------ reading
    def nm(self, *args: str, timeout: float = 10.0, secret: Optional[str] = None) -> Tuple[int, str]:
        rc, out = self._run(["nmcli", *args], timeout)
        return rc, scrub(out, secret)

    def device(self) -> Optional[Dict]:
        if self.fake:
            return {"device": "wlan0", "state": "connected", "connection": self._fake_connected}
        rc, out = self.nm("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
        if rc != 0:
            return None
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 4 and f[1] == "wifi" and (self.interface == "auto" or f[0] == self.interface):
                return {"device": f[0], "state": f[2], "connection": f[3]}
        return None

    def ipv4(self, dev: str) -> List[str]:
        if self.fake:
            return ["192.0.2.77/24"] if self._fake_connected else []
        rc, out = self.nm("-g", "IP4.ADDRESS", "device", "show", dev)
        return [a for a in re.split(r"[\s|]+", out.strip()) if "/" in a] if rc == 0 else []

    def status(self) -> Dict:
        if not self.enabled:
            return {"available": False, "reason": "Wi-Fi settings are turned off (wifi.enabled)"}
        d = self.device()
        if d is None:
            return {"available": False, "reason": "no Wi-Fi adapter managed by NetworkManager"}
        connected = d["state"].startswith("connected")
        return {"available": True, "device": d["device"], "state": d["state"],
                "connection": d["connection"] if connected else "",
                "ipv4": self.ipv4(d["device"]) if connected else [], "last_result": self.last_result}

    def scan(self, rescan="auto") -> List[Dict]:
        """Networks in range, one row per name. rescan: True = scan now, "auto" = only if the
        list is older than NetworkManager's limit (a page opening must not force a scan that
        stalls a Wi-Fi link in use), False = the cached list."""
        if self.fake:
            return [{"ssid": "lab-wifi", "signal": 78, "security": "WPA2", "in_use": self._fake_connected == "lab-wifi"},
                    {"ssid": "field-router", "signal": 55, "security": "WPA2 WPA3", "in_use": False},
                    {"ssid": "guest", "signal": 30, "security": "", "in_use": False}]
        d = self.device()
        if d is None:
            raise WifiError("no Wi-Fi adapter", 404)
        rc, out = self.nm("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                          "--rescan", "auto" if rescan == "auto" else ("yes" if rescan else "no"),
                          "ifname", d["device"], timeout=20)
        if rc != 0:
            raise WifiError(self._why(out, "scan failed"), 500)
        best: Dict[str, Dict] = {}
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 4 or not f[1]:
                continue                                  # hidden networks have no SSID to show
            try:
                signal = int(f[2])
            except ValueError:
                signal = 0
            item = {"ssid": f[1], "signal": signal, "security": f[3].strip(), "in_use": f[0].strip() == "*"}
            old = best.get(f[1])
            if old is None or item["in_use"] or (not old["in_use"] and signal > old["signal"]):
                best[f[1]] = item                         # one row per SSID (several access points)
        return sorted(best.values(), key=lambda i: (not i["in_use"], -i["signal"], i["ssid"]))

    def _wifi_profiles(self) -> Dict[str, str]:
        """uuid -> name of every saved Wi-Fi profile."""
        rc, out = self.nm("-t", "-f", "UUID,TYPE,NAME", "connection", "show")
        if rc != 0:
            return {}
        out_d = {}
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 3 and f[1] == WIFI_TYPE:
                out_d[f[0]] = f[2]
        return out_d

    def _active_wifi(self, dev: str) -> Optional[str]:
        rc, out = self.nm("-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active")
        if rc != 0:
            return None
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 3 and f[1] == WIFI_TYPE and f[2] == dev:
                return f[0]
        return None

    @staticmethod
    def _why(out: str, default: str) -> str:
        low = out.lower()
        if any(w in low for w in NO_PERMISSION):
            return PERMISSION_HINT
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        return lines[-1][:300] if lines else default

    # ------------------------------------------------------------------ change
    def connect(self, ssid: str, password: str = "", hidden: bool = False) -> Dict:
        """Connect to a network, keeping the previous one if this fails. Returns stages."""
        ssid = (ssid or "").strip("\n")
        if not 1 <= len(ssid.encode("utf-8")) <= 32:
            raise WifiError("the network name (SSID) must be 1 to 32 bytes")
        if password and not 8 <= len(password) <= 63:
            raise WifiError("a WPA password has 8 to 63 characters")
        if "\x00" in ssid or "\x00" in password:
            raise WifiError("invalid characters")
        if not self._lock.acquire(blocking=False):
            raise WifiError("another Wi-Fi change is in progress", 409)
        try:
            result = self._fake_connect(ssid, password) if self.fake else self._connect(ssid, password, hidden)
        finally:
            self._lock.release()
        self.last_result = {k: v for k, v in result.items() if k != "password"} | {"ssid": ssid, "at": time.time()}
        if result["success"]:
            self.events.add("success", "network", f"Wi-Fi connected to {ssid}", ", ".join(result.get("ipv4") or []))
        else:
            self.events.add("warning", "network", f"Wi-Fi connection to {ssid} failed", result["summary"])
        return result

    def _connect(self, ssid: str, password: str, hidden: bool) -> Dict:
        d = self.device()
        if d is None:
            raise WifiError("no Wi-Fi adapter managed by NetworkManager", 404)
        dev = d["device"]
        before = self._wifi_profiles()
        previous = self._active_wifi(dev)
        stages = {"saved": False, "associated": False, "ip": False}
        argv = ["--wait", str(int(self.connect_timeout_s)), "device", "wifi", "connect", ssid, "ifname", dev]
        if password:
            argv += ["password", password]
        if hidden:
            argv += ["hidden", "yes"]
        rc, out = self.nm(*argv, timeout=self.connect_timeout_s + 10, secret=password)
        created = [u for u in self._wifi_profiles() if u not in before]
        stages["saved"] = bool(created) or rc == 0
        if rc == 0:
            stages["associated"] = True
            deadline = self._clock() + self.ip_timeout_s
            while True:
                ips = self.ipv4(dev)
                if ips:
                    stages["ip"] = True
                    return {"success": True, "stages": stages, "ipv4": ips, "device": dev,
                            "summary": f"connected to {ssid} ({', '.join(ips)})"}
                if self._clock() >= deadline:
                    break
                self._sleep(1.0)
            why, code = f"no IPv4 address within {self.ip_timeout_s:.0f} s", "no_ip"
        else:
            why, code = self._why(out, "connection failed"), failure_code(out)
        restored = self._rollback(created, previous, dev)
        return {"success": False, "stages": stages, "device": dev, "code": code,
                "restored": self._restored, "kept": self._kept, "summary": f"{why}; {restored}"}

    def _rollback(self, created: List[str], previous: Optional[str], dev: str) -> str:
        """Delete only the Wi-Fi profiles this attempt created; bring the previous one back if it was left."""
        wifi = self._wifi_profiles()
        self._restored, self._kept = "", False            # the network it is (back) on, for the pages
        for uuid in created:
            if uuid in wifi:                              # never a wired or other profile
                self.nm("connection", "delete", "uuid", uuid)
        if previous:
            name = wifi.get(previous, previous)
            if self._active_wifi(dev) == previous:
                # Never left (e.g. the network was not found): "connection up" would drop it and
                # join again, cutting Wi-Fi for ~10 s (measured on the Jetson, 2026-10-21).
                self._restored, self._kept = name, True
                return f"still on {name}"
            rc, out = self.nm("--wait", str(int(self.connect_timeout_s)), "connection", "up", "uuid", previous,
                              timeout=self.connect_timeout_s + 10)
            self._restored = name if rc == 0 else ""
            return f"back on {name}" if rc == 0 else f"could not reconnect to {name}: {self._why(out, 'error')}"
        return "Wi-Fi left disconnected (it was not connected before)"

    def _fake_connect(self, ssid: str, password: str) -> Dict:
        self._sleep(1.0)
        if password == "wrong-password":
            return {"success": False, "stages": {"saved": True, "associated": False, "ip": False},
                    "code": "wrong_password", "restored": self._fake_connected or "",
                    "summary": "Secrets were required, but not provided; back on " + (self._fake_connected or "nothing")}
        self._fake_connected = ssid
        return {"success": True, "stages": {"saved": True, "associated": True, "ip": True},
                "ipv4": ["192.0.2.77/24"], "summary": f"connected to {ssid} (192.0.2.77/24)"}


# ---------------------------------------------------------------------- who may change it
def interface_of(addr: str) -> Optional[str]:
    """The interface this device uses to reach a client (its connection comes in the same way)."""
    try:
        r = subprocess.run(["ip", "-o", "route", "get", addr], capture_output=True, text=True,
                           timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\bdev (\S+)", r.stdout)
    return m.group(1) if m else None


def is_wired(dev: str, sysfs: str = "/sys/class/net") -> bool:
    """A real Ethernet port: ARP type 1 (Ethernet), no wireless directory, not a tunnel."""
    base = Path(sysfs) / dev
    if (base / "wireless").exists() or (base / "phy80211").exists():
        return False
    try:
        return (base / "type").read_text().strip() == "1" and not dev.startswith(("wl", "tun", "tap", "wg", "tailscale"))
    except OSError:
        return False


def client_path(addr: str, wifi_dev: Optional[str], route=interface_of, wired=is_wired) -> Tuple[bool, str]:
    """May this browser change Wi-Fi? Only if its connection does not depend on Wi-Fi."""
    import ipaddress
    try:
        if ipaddress.ip_address(addr).is_loopback:
            return True, "this computer"
    except ValueError:
        return False, "unknown client address"
    dev = route(addr)
    if dev is None:
        return False, "cannot tell how your computer is connected"
    if wifi_dev and dev == wifi_dev:
        return False, f"your computer is connected over Wi-Fi ({dev}); connect with a network cable to change Wi-Fi"
    if not wired(dev):
        return False, f"your computer is connected through {dev}, not an Ethernet cable; connect with a cable to change Wi-Fi"
    return True, f"Ethernet ({dev})"
