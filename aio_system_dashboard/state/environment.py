"""The environment the device is in: network, RTK, vehicle. Detected, and the pages may override.

The device moves between places with and without a network and RTK corrections, connected to a
vehicle or not (docs/DIRECTION.md). The other parts follow this one answer instead of guessing
each for themselves. Today it decides whether READY needs RTK; the vehicle answer is shown only.

* network: wired / wifi / none: a connection out works (network.internet_check), and the default
  route says over which interface.
* rtk: True when RTK corrections arrived or RTK float / fix was seen in this power-on (kept until
  the next power-on, so a short loss under a bridge is a loss, not a change of place). False when
  the receiver has no NTRIP set up, or has it but there is no network. Unknown otherwise: RTK
  is then required, the careful choice; it is decided within seconds after start-up.
* vehicle: True when aio_nav.yaml has a vehicle address (output_udp).

A detected value must hold ENV_HOLD_S before it replaces the previous one. A page override wins
(kept in state/environment.json until changed back to automatic); readiness.require_rtk: true /
false in the config pins RTK the same way when nobody overrode it.
"""

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

FILE = "environment.json"
OLD_RTK_FILE = "readiness.json"          # before 2026-11: the RTK switch alone
ENV_HOLD_S = 10.0
KEYS = ("rtk", "vehicle")                # the answers a page may override (the network is observed)
CORR_OK, CORR_WARN, CORR_ERROR, CORR_STALE = 0, 1, 2, 3     # diagnostic_msgs levels (gnss_diag)
RTK_QUALITIES = ("float", "fixed")


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def wireless(interface: str) -> bool:
    return bool(interface) and os.path.isdir(f"/sys/class/net/{interface}/wireless")


# ---------------------------------------------------------------- saved choices
def load(cfg) -> Dict:
    """The saved overrides and the RTK latch. An RTK switch saved before (readiness.json) counts as
    the rtk override until something new is saved."""
    data: Dict = {}
    try:
        data = json.loads(cfg.state_file(FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        pass
    if not isinstance(data, dict):
        data = {}
    if "rtk" not in data:
        try:
            old = json.loads(cfg.state_file(OLD_RTK_FILE).read_text(encoding="utf-8"))
            if isinstance(old.get("require_rtk"), bool):
                data["rtk"] = {"value": old["require_rtk"], "by": old.get("changed_by"),
                               "at": old.get("changed_at")}
        except (OSError, ValueError, AttributeError):
            pass
    return data


def _save(cfg, data: Dict) -> None:
    f = cfg.state_file(FILE)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    tmp.replace(f)


def set_override(cfg, key: str, value: Optional[bool], who: str) -> None:
    """value True / False: fixed by a person; None: back to automatic."""
    if key not in KEYS:
        raise ValueError(f"unknown environment setting {key!r}")
    data = load(cfg)
    data[key] = None if value is None else {"value": bool(value), "by": who, "at": time.time()}
    _save(cfg, data)
    if key == "rtk":
        try:
            cfg.state_file(OLD_RTK_FILE).unlink()         # its value now lives here
        except (OSError, AttributeError):
            pass


def override(cfg, key: str) -> Optional[Dict]:
    o = load(cfg).get(key)
    return o if isinstance(o, dict) and isinstance(o.get("value"), bool) else None


# ------------------------------------------------------------------ detection
def detect_network(network: Dict) -> Tuple[Optional[str], str]:
    """(wired | wifi | none | None, how it was decided)."""
    net = network.get("internet") or {}
    reach = net.get("reachable")
    if reach is None:
        return None, ("Internet check not set up (network.internet_check)" if net.get("error") == "not set up"
                      else "Not checked yet")
    if not reach:
        return "none", f"No connection out ({net.get('error') or 'unreachable'})"
    iface = (network.get("default_route") or {}).get("interface") or ""
    kind = "wifi" if wireless(iface) else "wired"
    return kind, f"Connection out works, over {iface or 'the default route'}"


def detect_rtk(gnss: Dict, diag: Dict, network_value: Optional[str], seen: bool) -> Tuple[Optional[bool], str]:
    """(True | False | None, how it was decided)."""
    if seen:
        return True, "RTK corrections or an RTK position in this power-on"
    level = (diag.get("corrections") or {}).get("level") if diag.get("available") else None
    if level == CORR_STALE:
        return False, "The receiver has no RTK corrections set up (NTRIP)"
    if level in (CORR_OK, CORR_WARN, CORR_ERROR):
        if network_value == "none":
            return False, "NTRIP is set up, but there is no network for the corrections"
        return True, "NTRIP is set up" + (" and the network works" if network_value else "")
    if network_value == "none":
        return False, "No network for RTK corrections"
    return None, "Waiting for the receiver's diagnostics"


def rtk_seen_now(gnss: Dict, diag: Dict) -> bool:
    corr = (diag.get("corrections") or {}) if diag.get("available") else {}
    return (corr.get("level") == CORR_OK or
            (bool(gnss.get("available")) and gnss.get("quality") in RTK_QUALITIES))


def detect_vehicle(cfg) -> Tuple[bool, str]:
    ext = [d for d in cfg.nav_destinations() if d.get("kind") == "external"]
    if ext:
        return True, f'Vehicle address {ext[0]["host"]}:{ext[0]["port"]} (output_udp in aio_nav.yaml)'
    return False, "No vehicle address (output_udp in aio_nav.yaml is empty)"


class _Held:
    """A detected value that changes only after the new one held ``hold_s``."""

    def __init__(self, hold_s: float):
        self.hold_s = hold_s
        self.value: Optional[object] = None
        self.how = ""
        self.decided = False
        self._cand: Tuple[Optional[object], Optional[float]] = (None, None)

    def update(self, value, how: str, now: float, at_once: bool = False):
        if not self.decided or value == self.value or at_once:
            self.value, self.how, self.decided = value, how, value is not None
            self._cand = (None, None)
            return self.value
        cand, since = self._cand
        if cand != value or since is None:
            self._cand = (value, now)
        elif now - since >= self.hold_s:
            self.value, self.how = value, how
            self._cand = (None, None)
        return self.value


class Environment:
    """Evaluated by the health step (5 Hz) from the store's network, gnss and gnss_diag sections."""

    def __init__(self, cfg, clock=time.monotonic, boot_id=_boot_id):
        self.cfg, self._clock, self._boot_id = cfg, clock, boot_id
        self._network = _Held(ENV_HOLD_S)
        self._rtk = _Held(ENV_HOLD_S)
        self._seen = load(cfg).get("rtk_seen_boot") == boot_id() and bool(boot_id())

    def _latch(self) -> None:
        self._seen = True
        data = load(self.cfg)
        data["rtk_seen_boot"] = self._boot_id()
        try:
            _save(self.cfg, data)
        except OSError:
            pass                          # kept in memory for this run

    def evaluate(self, network: Dict, gnss: Dict, diag: Dict, now: Optional[float] = None) -> Dict:
        now = self._clock() if now is None else now
        if not self._seen and rtk_seen_now(gnss, diag):
            self._latch()
        n_raw, n_how = detect_network(network)
        n_val = self._network.update(n_raw, n_how, now)
        r_val, r_how = detect_rtk(gnss, diag, n_raw, self._seen)     # held once, below, not twice
        r_val = self._rtk.update(r_val, r_how, now, at_once=self._seen)
        v_val, v_how = detect_vehicle(self.cfg)

        saved = load(self.cfg)
        out = {"network": {"value": n_val, "how": self._network.how or n_how, "auto": True},
               "rtk": self._answer("rtk", r_val, self._rtk.how or r_how, saved),
               "vehicle": self._answer("vehicle", v_val, v_how, saved)}
        out["rtk_required"], out["rtk_source"] = self._rtk_required(out["rtk"])
        return out

    def _answer(self, key: str, detected, how: str, saved: Dict) -> Dict:
        o = saved.get(key)
        if isinstance(o, dict) and isinstance(o.get("value"), bool):
            return {"value": o["value"], "auto": False, "detected": detected, "how": how,
                    "changed_by": o.get("by"), "changed_at": o.get("at")}
        return {"value": detected, "auto": True, "detected": detected, "how": how}

    def _rtk_required(self, rtk: Dict) -> Tuple[bool, str]:
        """READY needs RTK when the environment has it: a page override, else the config pinning
        it (readiness.require_rtk true / false), else the detection (unknown: required)."""
        if not rtk["auto"]:
            return rtk["value"], "set on a page"
        pinned = self.cfg["readiness"].get("require_rtk", "auto")
        if isinstance(pinned, bool):
            return pinned, "config"
        if rtk["value"] is None:
            return True, "environment not known yet"
        return rtk["value"], "environment"
