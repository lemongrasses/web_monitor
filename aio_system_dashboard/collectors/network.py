"""Network state: interfaces, default route, NAV destination route, device reachability."""

import ipaddress
import re
import socket
import subprocess
import time
from typing import Dict, List, Optional

import psutil

from .base import PeriodicCollector

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME_RE.match(host or "")) and not host.startswith("-")


def ping(host: str, timeout_s: float = 1.0) -> Dict:
    """Single ICMP echo using the system ping (host must come from config)."""
    if not valid_host(host):
        return {"reachable": False, "error": "invalid host"}
    t0 = time.monotonic()
    try:
        res = subprocess.run(["ping", "-n", "-c", "1", "-W", str(max(1, int(timeout_s))), host],
                             capture_output=True, text=True, timeout=timeout_s + 2, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"reachable": False, "error": str(e)}
    m = re.search(r"time[=<]([\d.]+)\s*ms", res.stdout)
    return {
        "reachable": res.returncode == 0,
        "rtt_ms": float(m.group(1)) if m else None,
        "elapsed_ms": (time.monotonic() - t0) * 1000.0,
    }


def default_route() -> Optional[Dict]:
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "00000000":
            gw = socket.inet_ntoa(int(parts[2], 16).to_bytes(4, "little"))
            return {"interface": parts[0], "gateway": gw}
    return None


def route_to(host: str) -> Dict:
    """`ip route get` for a configured destination: which interface/source IP it uses."""
    if not valid_host(host):
        return {"ok": False, "error": "invalid host"}
    try:
        res = subprocess.run(["ip", "-o", "route", "get", host], capture_output=True,
                             text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    if res.returncode != 0:
        return {"ok": False, "error": (res.stderr or res.stdout).strip()}
    dev = re.search(r"\bdev (\S+)", res.stdout)
    src = re.search(r"\bsrc (\S+)", res.stdout)
    via = re.search(r"\bvia (\S+)", res.stdout)
    return {"ok": True, "interface": dev.group(1) if dev else None,
            "source": src.group(1) if src else None, "via": via.group(1) if via else None}


def interfaces() -> List[Dict]:
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    out = []
    for name, st in sorted(stats.items()):
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
            continue
        ipv4 = [{"address": a.address, "netmask": a.netmask}
                for a in addrs.get(name, []) if a.family == socket.AF_INET]
        try:
            with open(f"/sys/class/net/{name}/carrier", encoding="utf-8") as f:
                carrier = f.read().strip() == "1"
        except OSError:
            carrier = None
        out.append({"name": name, "up": st.isup, "carrier": carrier,
                    "speed_mbps": st.speed or None, "mtu": st.mtu, "ipv4": ipv4})
    return out


class NetworkCollector(PeriodicCollector):
    name = "network"
    section = "network"

    def __init__(self, store, cfg, fake=None):
        super().__init__(store, cfg["network"]["ping_interval_s"])
        self.cfg = cfg
        self.fake = fake

    def check_device(self, key: str) -> Dict:
        dev = self.cfg["devices"].get(key, {})
        host = dev.get("host", "")
        if self.fake is not None:
            ok = self.fake.reachable(key)
            return {"host": host, "reachable": ok, "rtt_ms": 0.4 if ok else None,
                    "checked": time.time()}
        if not host:
            return {"host": "", "reachable": None, "error": "no host configured",
                    "checked": time.time()}
        result = ping(host)
        result.update({"host": host, "checked": time.time()})
        return result

    def _auto_interface(self, ifaces: List[Dict]) -> str:
        """Interface used to reach the sensors, else the default-route one, else the first link up."""
        for dev in self.cfg["devices"].values():
            host = (dev or {}).get("host")
            if host:
                r = route_to(host)
                if r.get("ok") and r.get("interface") and r["interface"] != "lo":
                    return r["interface"]
        dr = default_route()
        if dr:
            return dr["interface"]
        up = [i for i in ifaces if i["up"] and i["carrier"] and i["ipv4"]]
        return up[0]["name"] if up else ""

    def poll(self) -> Dict:
        ifaces = interfaces()
        sensor_if = self.cfg["network"].get("sensor_interface") or "auto"
        auto_if = sensor_if == "auto"
        if auto_if:
            sensor_if = self._auto_interface(ifaces)
        sensor = next((i for i in ifaces if i["name"] == sensor_if), None)
        routes = {}
        for d in self.cfg.nav_destinations():
            if d["kind"] == "external":
                routes[f'{d["host"]}:{d["port"]}'] = route_to(d["host"])
        return {
            "interfaces": ifaces,
            "sensor_interface": sensor_if,
            "sensor_interface_auto": auto_if,
            "sensor_link_up": (bool(sensor["up"] and sensor["carrier"] is not False)
                               if sensor else None),
            "default_route": default_route(),
            "nav_routes": routes,
            "devices": {k: self.check_device(k) for k in self.cfg["devices"]},
        }
