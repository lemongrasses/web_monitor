"""Is the vehicle still? The limits, the judgement, and measuring the limits on the vehicle.

AIO NAV is (re)started automatically only with the vehicle standing (actions/autostart.py): its
first alignment step needs it, and a crash or a reboot may happen while driving.

judge(): the IMU's largest rotation rate and the spread of its acceleration magnitude over its
newest samples (collectors/ros2.py) under their limits and, while the GNSS receiver has a
position, its speed (collectors/speed.py) under the speed limit. An IMU alone cannot tell standing
from driving straight at a steady speed on a smooth road; the speed can. Without a GNSS position
the receiver's speed drifts or freezes, so it is not used then.

Limits: the readiness.still_* settings, unless measured on the vehicle (Maintenance > Still
detection) and kept in state/still_limits.json, across restarts, reboots and reinstalls. Every
vehicle shakes differently when parked with the engine running: a recorded drive (2026-09-23)
showed parked < 0.035 rad/s and < 0.16 m/s2 (above the 0.15 default), driving > 0.17 rad/s and
> 0.6 m/s2.

StillCalibration: "still" records the vehicle parked with the engine running and proposes MARGIN
times the largest values seen, within RANGES; "moving" (optional) records slow driving and counts
how often the limits would have called it still.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

KEYS = ("gyro_rad_s", "accel_std", "speed_m_s")
NAMES = {"gyro_rad_s": "rotation", "accel_std": "shake", "speed_m_s": "GNSS speed"}
UNITS = {"gyro_rad_s": "rad/s", "accel_std": "m/s2", "speed_m_s": "m/s"}
# (lowest, highest) a limit may be. The highest stay below what driving showed (0.17 rad/s,
# 0.6 m/s2) and below walking pace (1.4 m/s). A proposal is never below FLOORS.
RANGES = {"gyro_rad_s": (0.005, 0.15), "accel_std": (0.01, 0.5), "speed_m_s": (0.05, 0.5)}
FLOORS = {"gyro_rad_s": 0.02, "accel_std": 0.05, "speed_m_s": 0.15}
MARGIN = 2.0
FRESH_S = 1.0           # a reading older than this belongs to an earlier sampling window
BUCKET_S = 2.0          # a measurement keeps one reading per 2 s (the IMU judges 2 s of samples)
SPEED_MAX_AGE_S = 10.0  # like the IMU's MOTION_MAX_AGE_S
GNSS_POSITION = ("fixed", "float", "spp", "unrated", "unrated_poor")
DURATION_S = {"still": 60.0, "moving": 30.0}
MIN_READINGS = {"still": 5, "moving": 3}


def defaults(cfg) -> Dict[str, float]:
    r = cfg["readiness"]
    return {"gyro_rad_s": float(r["still_gyro_rad_s"]), "accel_std": float(r["still_accel_std"]),
            "speed_m_s": float(r["still_speed_m_s"])}


def check_values(values) -> Dict[str, float]:
    """The three limits as numbers within RANGES, or ValueError saying which is wrong."""
    if not isinstance(values, dict):
        raise ValueError("the limits must be given as numbers")
    out = {}
    for k in KEYS:
        lo, hi = RANGES[k]
        try:
            v = float(values[k])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{NAMES[k]} limit missing or not a number")
        if not lo <= v <= hi:
            raise ValueError(f"{NAMES[k]} limit must be between {lo} and {hi} {UNITS[k]}")
        out[k] = round(v, 4)
    return out


def usable_speed(speed: Optional[Dict], gnss: Optional[Dict]) -> Tuple[Optional[float], str]:
    """(speed in m/s, "") when it can be trusted, else (None, why not)."""
    if not speed or not speed.get("available"):
        return None, (speed or {}).get("reason") or "no speed data"
    if speed.get("frozen"):
        return None, speed.get("reason") or "the receiver repeats the same speed"
    if (speed.get("age_s") or 0.0) > SPEED_MAX_AGE_S:
        return None, "speed data too old"
    if (gnss or {}).get("quality") not in GNSS_POSITION:
        return None, "no GNSS position (the receiver's speed is not reliable without one)"
    return float(speed["speed_max"]), ""


def judge(motion: Optional[Dict], speed: Optional[Dict], gnss: Optional[Dict], limits: Dict) -> Dict:
    sp, note = usable_speed(speed, gnss)
    out = {"limits": {k: limits[k] for k in KEYS}, "speed": sp, "speed_used": sp is not None, "speed_note": note}
    if not motion:
        return dict(out, still=None, reason="no IMU data")
    g_ok = motion["gyro_max"] < limits["gyro_rad_s"]
    a_ok = motion["accel_std"] < limits["accel_std"]
    s_ok = sp is None or sp < limits["speed_m_s"]
    reason = ("" if g_ok and a_ok and s_ok else
              f"moving: rotation {motion['gyro_max']:.2f} rad/s" if not g_ok else
              f"moving: acceleration varies {motion['accel_std']:.2f} m/s2" if not a_ok else
              f"moving: GNSS speed {sp:.2f} m/s")
    return dict(out, still=g_ok and a_ok and s_ok, reason=reason, gyro_max=motion["gyro_max"],
                accel_std=motion["accel_std"], age_s=motion.get("age_s"))


def _fmt(values: Dict) -> str:
    return ", ".join(f"{NAMES[k]} {values[k]:g} {UNITS[k]}" for k in KEYS if values.get(k) is not None)


class StillLimits:
    """The limits in use: measured or typed on the Maintenance page, else the settings."""

    def __init__(self, cfg, path, events=None):
        self.cfg, self.path, self.events = cfg, Path(path), events
        self._lock = threading.Lock()
        self._saved = self._load()

    def _event(self, level: str, title: str, detail: str) -> None:
        if self.events is not None:
            self.events.add(level, "readiness", title, detail)

    def _load(self) -> Optional[Dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            values = check_values(data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            self._event("warning", "Still limits not usable",
                        f"{self.path.name}: {e}; the defaults from the settings are used")
            return None
        return dict(data, **values)

    def get(self) -> Dict:
        with self._lock:
            saved = self._saved
        if saved is None:
            return dict(defaults(self.cfg), how="default", at=None, by=None, measured=None)
        return dict({k: saved[k] for k in KEYS}, how=saved.get("how") or "typed", at=saved.get("at"),
                    by=saved.get("by"), measured=saved.get("measured"))

    def apply(self, values, how: str, by: str = "", measured: Optional[Dict] = None) -> Dict:
        new = check_values(values)
        old = self.get()
        record = dict(new, how=how, at=time.time(), by=by, measured=measured)
        self._write(record)
        with self._lock:
            self._saved = record
        self._event("warning", "Still limits changed", f"{_fmt(old)} -> {_fmt(new)} ({how}, from {by or '?'})")
        return self.get()

    def reset(self, by: str = "") -> Dict:
        old = self.get()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        with self._lock:
            self._saved = None
        self._event("warning", "Still limits back to the defaults", f"{_fmt(old)} -> {_fmt(self.get())} (from {by or '?'})")
        return self.get()

    def _write(self, record: Dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class StillCalibration:
    """One measurement at a time, in its own thread. inputs() returns (IMU motion, speed, gnss)
    as judge() takes them."""

    def __init__(self, inputs: Callable[[], Tuple], limits: StillLimits, clock=time.monotonic, poll_s: float = 0.5):
        self.inputs, self.limits, self._clock, self.poll_s = inputs, limits, clock, poll_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._run: Optional[Dict] = None
        self.proposal: Optional[Dict] = None     # from the last good "still" measurement
        self.measured: Optional[Dict] = None     # its largest values

    # ------------------------------------------------------------------ control
    def start(self, kind: str) -> Dict:
        if kind not in DURATION_S:
            raise ValueError("unknown measurement")
        motion, _, _ = self.inputs()
        if not motion:
            raise ValueError("no IMU data: the IMU must be publishing to measure")
        with self._lock:
            if self._run and self._run["state"] == "running":
                raise RuntimeError("a measurement is already running")
            self._stop.clear()
            self._run = {"kind": kind, "state": "running", "started": self._clock(), "ended": None,
                         "duration_s": DURATION_S[kind], "readings": {}, "last": None,
                         "result": None, "message": ""}
            if kind == "still":
                self.proposal = self.measured = None
        threading.Thread(target=self._measure, name="still-calibration", daemon=True).start()
        return self.status()

    def cancel(self) -> Dict:
        self._stop.set()
        return self.status()

    def clear_proposal(self) -> None:
        """After the limits were applied or reset: the proposal is used up."""
        with self._lock:
            self.proposal = self.measured = None

    def status(self) -> Dict:
        with self._lock:
            run = dict(self._run) if self._run else None
            proposal, measured = self.proposal, self.measured
        if run is None:
            return {"state": "idle", "proposal": proposal, "measured": measured}
        now = run["ended"] if run["ended"] is not None else self._clock()
        return {"kind": run["kind"], "state": run["state"], "elapsed_s": round(now - run["started"], 1),
                "duration_s": run["duration_s"], "readings": len(run["readings"]), "last": run["last"],
                "result": run["result"], "message": run["message"], "proposal": proposal, "measured": measured}

    # ------------------------------------------------------------------ measuring
    def _measure(self) -> None:
        with self._lock:
            run = self._run
        seen = None
        while not self._stop.wait(self.poll_s):
            elapsed = self._clock() - run["started"]
            if elapsed >= run["duration_s"]:
                break
            motion, speed, gnss = self.inputs()
            if not motion or (motion.get("age_s") or 0.0) > FRESH_S:
                continue
            sp, _ = usable_speed(speed, gnss)
            if sp is not None and (speed.get("age_s") or 0.0) > FRESH_S:
                sp = None
            key = (motion["gyro_max"], motion["accel_std"], motion.get("samples"))
            with self._lock:
                run["last"] = {"gyro_rad_s": motion["gyro_max"], "accel_std": motion["accel_std"], "speed_m_s": sp}
                if key == seen:
                    continue                     # the same window read again
                seen = key
                b = run["readings"].setdefault(int(elapsed // BUCKET_S),
                                               {"gyro_rad_s": 0.0, "accel_std": 0.0, "speed_m_s": None})
                b["gyro_rad_s"] = max(b["gyro_rad_s"], motion["gyro_max"])
                b["accel_std"] = max(b["accel_std"], motion["accel_std"])
                if sp is not None:
                    b["speed_m_s"] = max(b["speed_m_s"] or 0.0, sp)
            if run["kind"] == "still" and motion["gyro_max"] > RANGES["gyro_rad_s"][1]:
                self._finish(run, "failed", None, f"The vehicle moved: rotation reached {motion['gyro_max']:.3f} "
                                                  f"rad/s. Park it, keep the engine running, and measure again.")
                return
        if self._stop.is_set():
            self._finish(run, "cancelled", None, "Measurement cancelled.")
            return
        readings = list(run["readings"].values())
        if len(readings) < MIN_READINGS[run["kind"]]:
            self._finish(run, "failed", None, f"Only {len(readings)} IMU readings in {run['duration_s']:.0f} s "
                                              f"(at least {MIN_READINGS[run['kind']]} needed): is the IMU publishing?")
            return
        if run["kind"] == "still":
            self._still_result(run, readings)
        else:
            self._moving_result(run, readings)

    def _finish(self, run: Dict, state: str, result: Optional[Dict], message: str,
                proposal: Optional[Dict] = None, measured: Optional[Dict] = None) -> None:
        with self._lock:
            run.update(state=state, result=result, message=message, ended=self._clock())
            if proposal is not None:
                self.proposal, self.measured = proposal, measured
        logger.info("still measurement (%s) %s: %s", run["kind"], state, message)

    def _still_result(self, run: Dict, readings) -> None:
        speeds = [r["speed_m_s"] for r in readings if r["speed_m_s"] is not None]
        worst = {"gyro_rad_s": max(r["gyro_rad_s"] for r in readings),
                 "accel_std": max(r["accel_std"] for r in readings),
                 "speed_m_s": max(speeds) if speeds else None}
        for k in KEYS:
            if worst[k] is not None and worst[k] > RANGES[k][1]:
                why = ("the vehicle moved" if k != "accel_std" else
                       "the vehicle moved, or its mounting shakes too much to tell standing from driving")
                self._finish(run, "failed", {"measured": worst},
                             f"The {NAMES[k]} reached {worst[k]:.3f} {UNITS[k]}, above the highest limit "
                             f"({RANGES[k][1]} {UNITS[k]}): {why}. Measure again with the vehicle parked.")
                return
        current = self.limits.get()
        proposal, notes = {}, []
        for k in KEYS:
            if worst[k] is None:
                proposal[k] = current[k]
                notes.append(f"GNSS speed was not available during the measurement, so its limit stays "
                             f"{current[k]:g} m/s.")
                continue
            lo, hi = RANGES[k]
            proposal[k] = round(min(hi, max(FLOORS[k], worst[k] * MARGIN)), 3)
            if worst[k] * MARGIN > hi:
                notes.append(f"The {NAMES[k]} is high when parked: its limit is at the highest allowed, {hi:g} {UNITS[k]}.")
        self._finish(run, "done", {"measured": worst, "proposal": proposal, "readings": len(readings)},
                     " ".join([f"Largest while still: {_fmt(worst)}. Proposed limits ({MARGIN:g} x, within the "
                               f"allowed range): {_fmt(proposal)}."] + notes),
                     proposal=proposal, measured=worst)

    def _moving_result(self, run: Dict, readings) -> None:
        with self._lock:
            limits = dict(self.proposal) if self.proposal else {k: self.limits.get()[k] for k in KEYS}
            which = "proposed" if self.proposal else "current"
        still = [r for r in readings
                 if r["gyro_rad_s"] < limits["gyro_rad_s"] and r["accel_std"] < limits["accel_std"]
                 and (r["speed_m_s"] is None or r["speed_m_s"] < limits["speed_m_s"])]
        speeds = [r["speed_m_s"] for r in readings if r["speed_m_s"] is not None]
        lowest = {"gyro_rad_s": min(r["gyro_rad_s"] for r in readings),
                  "accel_std": min(r["accel_std"] for r in readings),
                  "speed_m_s": min(speeds) if speeds else None}
        result = {"limits": limits, "which": which, "readings": len(readings), "counted_still": len(still),
                  "lowest": lowest, "speed_readings": len(speeds)}
        n = len(readings)
        if len(still) == n:
            msg = (f"Every reading counted as still with the {which} limits: was the vehicle moving? "
                   f"Lowest while driving: {_fmt(lowest)}.")
            state = "warning"
        elif still:
            msg = (f"{len(still)} of {n} readings while driving would count as still with the {which} limits "
                   f"(lowest: {_fmt(lowest)}). At that pace the vehicle can look still"
                   + ("" if speeds else ", and GNSS speed was not available to tell")
                   + ". Lower the limits, or check again a little faster.")
            state = "warning"
        else:
            msg = (f"None of the {n} readings while driving counted as still with the {which} limits "
                   f"(lowest: {_fmt(lowest)}): they tell driving from standing.")
            state = "done"
        self._finish(run, state, result, msg)
