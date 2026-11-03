"""GNSS / RTK troubleshooting from the receiver driver's diagnostics topic.

The OpenRTK330 driver publishes ``gnss/diagnostics`` (diagnostic_msgs/DiagnosticArray, 1 Hz) with
three statuses, each with key/value details:

    "openrtk330: GNSS solution"   the receiver's own solution (g1): fix, satellites used, diffage
    "openrtk330: GNSS signal"     satellites and C/N0 per constellation (its raw RTCM3 output)
    "openrtk330: RTK corrections" NTRIP state, last error, bytes/s from the caster and to the receiver

``diagnose`` turns one snapshot into findings that name the likely cause, in the order an RTK
problem is checked: the receiver, the antenna signal, the corrections, then whether the receiver
uses them. Thresholds follow measurements on this system: with most signals at 20-28 dBHz the
receiver stays at SPP whatever corrections it gets; RTK float came with most signals >= 35 dBHz.
A correction drop or recovery is also logged as an event.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

OK, WARN, ERROR, STALE = 0, 1, 2, 3          # diagnostic_msgs/DiagnosticStatus levels
SECTIONS = {"GNSS solution": "solution", "GNSS signal": "signal", "RTK corrections": "corrections"}
SYSTEMS = ("GPS", "GLONASS", "Galileo", "BeiDou", "QZSS", "SBAS", "NavIC")
# The receiver gets good corrections and good signal but stays at SPP for this long: it is
# probably not using them (RTK float normally comes within a minute or two).
NOT_USED_AFTER_S = 180.0


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_status(level: int, message: str, values: Dict[str, str]) -> Dict:
    """One DiagnosticStatus as a dict; numbers converted, per-system values grouped."""
    out: Dict = {"level": int(level), "message": message or ""}
    systems: Dict[str, Dict] = {}
    for k, v in values.items():
        if "." in k and k.split(".", 1)[0] in SYSTEMS:
            name, field = k.split(".", 1)
            systems.setdefault(name, {})[field] = _num(v)
        elif k in ("fix", "state", "caster", "mountpoint", "last_error"):
            out[k] = v
        else:
            out[k] = _num(v)
    if systems:
        out["systems"] = [dict(name=n, **systems[n]) for n in SYSTEMS if n in systems]
    return out


def error_hint(error: str) -> str:
    """What a NTRIP failure usually means here."""
    e = (error or "").lower()
    if "401" in e or "unauthorized" in e:
        return ("The caster refused the login: a wrong account or password, or the same account is "
                "still logged in from an earlier connection (it clears by itself within about a minute).")
    if "sourcetable" in e or "mountpoint" in e:
        return "The mountpoint name is wrong (NTRIP_MOUNT in the driver's config.env)."
    if "dns" in e:
        return "The caster's name cannot be resolved: no DNS on the router's uplink."
    if any(s in e for s in ("unreachable", "no route", "timed out", "connect failed")):
        return ("No internet connection to the caster. Check the router's uplink (mobile signal, SIM, "
                "data plan): the router itself may still answer while its internet is down.")
    if "stale" in e or "closed" in e:
        return "The caster stopped sending; the driver reconnects by itself."
    return ""


def diagnose(snap: Dict) -> List[Dict]:
    """Findings, most important first: {level: fault|warning|info, title, detail, hint}."""
    if not snap.get("available"):
        return [{"level": "warning", "title": "No GNSS diagnostics from the receiver driver",
                 "detail": snap.get("reason", ""),
                 "hint": "The OpenRTK330 driver publishes them from the version with RTK diagnostics "
                         "(aio_install modules/openrtk-ros-driver). Check that its service runs."}]
    out: List[Dict] = []
    sol, sig, cor = (snap.get(k) or {} for k in ("solution", "signal", "corrections"))

    if sol.get("level") == ERROR:
        out.append({"level": "fault", "title": "The receiver sends no GNSS solution",
                    "detail": sol.get("message", ""),
                    "hint": "Check the OpenRTK330's power and its USB serial cable (ttyUSB0)."})

    if sig.get("level") == ERROR:
        out.append({"level": "fault", "title": "Very weak satellite signal",
                    "detail": _signal_text(sig),
                    "hint": "RTK needs most signals at 35 dBHz or more (open sky gives 40-50). Move the "
                            "antenna to open sky, then check its cable, connectors and antenna power."})
    elif sig.get("level") == WARN:
        out.append({"level": "warning", "title": "Weak satellite signal", "detail": _signal_text(sig),
                    "hint": "Partly blocked sky or multipath: RTK may take long or stay at float. "
                            "Mount the antenna high, clear of walls and large metal parts."})
    elif sig.get("level") == STALE:
        out.append({"level": "warning", "title": "No signal-strength data",
                    "detail": sig.get("message", ""),
                    "hint": "The driver reads the receiver's RTCM3 output on its second UART (RAW_PORT). "
                            "Another program reading that port takes the data away."})

    level = cor.get("level")
    if level == ERROR:
        err = cor.get("last_error") or ""
        out.append({"level": "fault", "title": "No RTK corrections",
                    "detail": cor.get("message", ""),
                    "hint": error_hint(err) or "The driver retries every few seconds."})
    elif level == WARN:
        out.append({"level": "warning", "title": "RTK corrections stalled",
                    "detail": cor.get("message", ""), "hint": error_hint(cor.get("last_error") or "")})
    elif level == STALE:
        out.append({"level": "info", "title": "RTK is not enabled", "detail": cor.get("message", ""),
                    "hint": "Set NTRIP_HOST, NTRIP_PORT and NTRIP_MOUNT in the driver's config.env."})

    if (snap.get("not_used_s") or 0) >= NOT_USED_AFTER_S:
        out.append({"level": "warning", "title": "Corrections arrive but the receiver does not use them",
                    "detail": f"good signal and corrections for {snap['not_used_s'] / 60:.0f} min, "
                              f"still {sol.get('fix', 'SPP')}, diffage {sol.get('diffage_s')}",
                    "hint": "Check the receiver's RTK settings and that the corrections go to the UART "
                            "it reads them on."})
    elif sol.get("fix") == "RTK float" and not out:
        out.append({"level": "info", "title": "RTK float, not fixed yet",
                    "detail": f"{sol.get('satellites_used'):.0f} satellites used"
                              if sol.get("satellites_used") is not None else "",
                    "hint": "Fixing needs a clear sky and corrections without breaks for a while."})
    return out


def _signal_text(sig: Dict) -> str:
    med, pct = sig.get("cnr_median_dbhz"), sig.get("pct_ge35")
    if med is None or pct is None:
        return sig.get("message", "")
    return f"C/N0 median {med:.0f} dBHz, {pct:.0f}% of signals at 35 dBHz or more"


class GnssDiagnostics:
    section = "gnss_diag"

    def __init__(self, cfg, store, events=None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.g = cfg["gnss_diag"]
        self.store, self.events, self._clock, self._wall = store, events, clock, wall
        self._lock = threading.Lock()
        self._last: Optional[Dict] = None
        self._last_at: Optional[float] = None
        self._count = 0
        self._history: deque = deque(maxlen=max(int(float(self.g["history_s"])), 1))
        self._not_used_since: Optional[float] = None
        self._receiving: Optional[bool] = None

    def on_array(self, statuses) -> None:
        """statuses: (name, level, message, {key: value}) for each DiagnosticStatus."""
        parsed: Dict = {}
        for name, level, message, values in statuses:
            for suffix, key in SECTIONS.items():
                if name.endswith(suffix):
                    parsed[key] = parse_status(level, message, values)
        if not parsed:
            return
        with self._lock:
            self._last, self._last_at = parsed, self._clock()
            self._count += 1

    def snapshot(self) -> Dict:
        now = self._clock()
        with self._lock:
            age = None if self._last_at is None else now - self._last_at
            if age is None or age > float(self.g["timeout_s"]):
                reason = (f"no message on {self.g['topic']} yet" if self._count == 0 else
                          f"no message on {self.g['topic']} for {age:.0f} s")
                snap = {"available": False, "reason": reason, "topic": self.g["topic"]}
            else:
                snap = {"available": True, "topic": self.g["topic"], "age_s": age, **self._last}
            snap["not_used_s"] = self._not_used(snap, now)
            snap["history"] = list(self._history)
        snap["findings"] = diagnose(snap)
        levels = [f["level"] for f in snap["findings"]]
        snap["level"] = ("fault" if "fault" in levels else "warning" if "warning" in levels else
                         "healthy" if snap["available"] else "unknown")
        return snap

    def _not_used(self, snap: Dict, now: float) -> float:
        """How long good corrections and good signal have not given RTK (called under the lock)."""
        sol, sig, cor = (snap.get(k) or {} for k in ("solution", "signal", "corrections"))
        suspicious = (snap["available"] and cor.get("level") == OK and sig.get("level") == OK
                      and sol.get("fix") == "SPP" and not sol.get("diffage_s"))
        if not suspicious:
            self._not_used_since = None
            return 0.0
        if self._not_used_since is None:
            self._not_used_since = now
        return now - self._not_used_since

    def tick(self) -> None:
        snap = self.snapshot()
        if snap["available"]:
            sol, sig, cor = (snap.get(k) or {} for k in ("solution", "signal", "corrections"))
            with self._lock:
                self._history.append({"ts": self._wall(), "fix": sol.get("fix"),
                                      "satellites_used": sol.get("satellites_used"),
                                      "cnr_median_dbhz": sig.get("cnr_median_dbhz"),
                                      "pct_ge35": sig.get("pct_ge35"),
                                      "caster_rate_Bps": cor.get("caster_rate_Bps")})
                snap["history"] = list(self._history)
            self._track_corrections(cor)
        self.store.set(self.section, snap)

    def _track_corrections(self, cor: Dict) -> None:
        if "level" not in cor or cor["level"] == STALE:
            return
        receiving = cor["level"] == OK
        if self.events is not None and self._receiving is not None and receiving != self._receiving:
            if receiving:
                self.events.add("success", "gnss", "RTK corrections restored", cor.get("caster", ""))
            else:
                self.events.add("warning", "gnss", "RTK corrections lost", cor.get("message", ""))
        self._receiving = receiving

    def attach(self, node, qos) -> None:
        """Subscribe on the ROS monitor's node (one small message a second)."""
        from diagnostic_msgs.msg import DiagnosticArray

        def on_msg(m):
            self.on_array([(s.name, s.level[0] if isinstance(s.level, bytes) else s.level, s.message,
                            {kv.key: kv.value for kv in s.values}) for s in m.status])
        node.create_subscription(DiagnosticArray, self.g["topic"], on_msg, qos)
        node.create_timer(1.0, self.tick)
        logger.info("GNSS diagnostics from %s", self.g["topic"])
