"""Readiness: one machine-readable answer to "may the vehicle use the navigation output now?".

The product state (health.py: STOPPED / STARTING / INITIALIZING / READY / FAULT) stays what the
pages show. This module derives from it, with no I/O and no clock of its own:

* a finer state for the vehicle and the operator (which alignment phase, degraded after READY,
  stopped for maintenance),
* the action the operator should take (hold still, drive straight, ...),
* bit masks: which health conditions hold, and every reason that forbids using the output,
* optional extra conditions for READY (GNSS quality, DSO odometry, IMU topic, a route to the
  vehicle). None of them is required unless ``readiness.require`` lists it, so by default
  READY means exactly what it meant before.

The same snapshot feeds /api/readiness, the pages, and the 0x05 status packet. Bit numbers and
codes are part of the 0x05 protocol (docs/OPERATION.md, "Status packet"): append, never renumber.
"""

from typing import Dict, List, Optional

SCHEMA_VERSION = 1

STATES = ("BOOTING", "CHECKING", "NAV_STARTING", "KEEP_STILL", "MOVE_STRAIGHT", "REFINING",
          "READY", "DEGRADED", "FAULT", "MAINTENANCE")
STATE_CODE = {s: i for i, s in enumerate(STATES)}

ACTIONS = ("NONE", "HOLD_STILL", "DRIVE_STRAIGHT", "KEEP_DRIVING", "SAFE_STOP", "SERVICE_REQUIRED",
           "MAINTENANCE_HOLD")
ACTION_CODE = {a: i for i, a in enumerate(ACTIONS)}
ACTION_TEXT = {
    "NONE": "",
    "HOLD_STILL": "Keep the vehicle completely still.",
    "DRIVE_STRAIGHT": "Drive straight at a steady speed in a safe, open area.",
    "KEEP_DRIVING": "Keep driving normally with GNSS available.",
    "SAFE_STOP": "Stop using navigation and follow the vehicle's safety procedure.",
    "SERVICE_REQUIRED": "Maintenance is needed.",
    "MAINTENANCE_HOLD": "Stopped. Press Start to resume.",
}

# health_flags: a condition that holds. inhibit_flags: a reason the output must not be used.
HEALTH_BITS = {
    "DASHBOARD_OK": 0, "CONFIG_OK": 1, "IMU_STREAM_OK": 2, "GNSS_STREAM_OK": 3,
    "GNSS_QUALITY_OK": 4, "NAV_PROCESS_OK": 5, "NAV_OUTPUT_OK": 6, "ALIGNMENT_OK": 7,
    "HEADING_OK": 8, "FINE_ALIGNMENT_OK": 9, "DSO_OK": 10, "EXTERNAL_ROUTE_OK": 11,
    "RTK_CORRECTION_OK": 12, "STORAGE_OK": 13, "VEHICLE_ACK_OK": 14, "TIME_SYNC_OK": 15,
    "NO_DUPLICATE_PROCESS": 16, "WIFI_OK": 17,
}
INHIBIT_BITS = {
    "BOOT_IN_PROGRESS": 0, "CONFIG_INVALID": 1, "IMU_MISSING_OR_STALE": 2,
    "GNSS_MISSING_OR_STALE": 3, "GNSS_QUALITY_LOW": 4, "NAV_NOT_RUNNING": 5, "NAV_OUTPUT_STALE": 6,
    "KEEP_STILL_REQUIRED": 7, "MOVE_STRAIGHT_REQUIRED": 8, "FINE_ALIGNMENT_REQUIRED": 9,
    "DSO_NOT_READY": 10, "EXTERNAL_ROUTE_DOWN": 11, "RTK_CORRECTION_LOST": 12,
    "STORAGE_NOT_WRITABLE": 13, "MAINTENANCE_HOLD": 14, "STARTUP_TIMEOUT": 15,
    "OWNERSHIP_CONFLICT": 16, "INTERNAL_FAULT": 17,
}

# Optional READY conditions: config name -> (health bit, inhibit bit, label)
CONDITIONS = {
    "imu": ("IMU_STREAM_OK", "IMU_MISSING_OR_STALE", "IMU data"),
    "gnss": ("GNSS_STREAM_OK", "GNSS_MISSING_OR_STALE", "GNSS data"),
    "gnss_quality": ("GNSS_QUALITY_OK", "GNSS_QUALITY_LOW", "GNSS quality"),
    "dso": ("DSO_OK", "DSO_NOT_READY", "Visual odometry (DSO)"),
    "vehicle_route": ("EXTERNAL_ROUTE_OK", "EXTERNAL_ROUTE_DOWN", "Route to the vehicle"),
    "storage": ("STORAGE_OK", "STORAGE_NOT_WRITABLE", "Storage"),
}

# Receiver quality, worst to best. An "unrated" fix (status -1, but a good position) occurs
# inside RTK float periods with this driver (collectors/gnss_monitor.py), so it counts as float;
# a poor unrated fix counts as SPP.
GNSS_RANK = {None: 0, "none": 1, "spp": 2, "unrated_poor": 2, "float": 3, "unrated": 3, "fixed": 4}
GNSS_CODE_NAMES = ("unknown", "none", "spp", "rtk_float", "rtk_fix")      # 0x05 gnss_quality
MIN_QUALITY = {"spp": 2, "float": 3, "rtk_float": 3, "fixed": 4, "fix": 4, "rtk_fix": 4}

PHASE_OF_STEP = {"alignment": ("KEEP_STILL", "KEEP_STILL_REQUIRED", "HOLD_STILL"),
                 "heading_valid": ("MOVE_STRAIGHT", "MOVE_STRAIGHT_REQUIRED", "DRIVE_STRAIGHT"),
                 "fine_alignment": ("REFINING", "FINE_ALIGNMENT_REQUIRED", "KEEP_DRIVING")}


def gnss_rank(quality: Optional[str]) -> int:
    return GNSS_RANK.get(quality, 0)


def mask(names, table) -> int:
    out = 0
    for n in names:
        out |= 1 << table[n]
    return out


def names_of(value: int, table) -> List[str]:
    return [n for n, b in sorted(table.items(), key=lambda kv: kv[1]) if value >> b & 1]


def evaluate(inp: Dict) -> Dict:
    """Pure evaluation. ``inp`` keys (all optional, missing = not known):

    product_state   the debounced product state (health.py)
    product_reasons its text lines; step: the current alignment step dict (key = flag name)
    nav_running     AIO NAV process seen; nav_fresh: 0x04 within stale_fault_s
    nav_flags       dict of alignment flags from the latest 0x04
    ever_output     a 0x04 arrived since AIO NAV started; ever_ready: READY was reached by the
                    current AIO NAV process (a later loss is DEGRADED, never back to the alignment flow)
    wanted          AIO NAV should be running (started, not stopped by an operator)
    booting         dashboard just started
    conditions      {name: {"required": bool, "ok": bool|None, "ready": bool, "reason": str}}
                    ok = holds now; ready = held long enough to allow READY
    hold            stopped on purpose (maintenance hold)
    autostart       {"enabled": bool, "state": str, "detail": str} from the start-up coordinator
    needs_service   the product fault is one only a person can fix (not installed, port taken)
    ownership       None, or a text: two managers / two processes for AIO NAV
    config_ok       the AIO NAV config file was read
    """
    st = inp.get("product_state") or "UNKNOWN"
    flags = inp.get("nav_flags") or {}
    conds: Dict[str, Dict] = inp.get("conditions") or {}
    auto = inp.get("autostart") or {}
    health, inhibit, reasons = {"DASHBOARD_OK"}, set(), []

    if inp.get("config_ok", True):
        health.add("CONFIG_OK")
    if inp.get("nav_running"):
        health.add("NAV_PROCESS_OK")
    else:
        inhibit.add("NAV_NOT_RUNNING")
    if inp.get("nav_fresh"):
        health.add("NAV_OUTPUT_OK")
    elif inp.get("nav_running"):
        inhibit.add("NAV_OUTPUT_STALE")
    required_flags = inp.get("nav_required_flags") or ("alignment", "heading_valid", "fine_alignment")
    for flag, bit, wait in (("alignment", "ALIGNMENT_OK", "KEEP_STILL_REQUIRED"),
                            ("heading_valid", "HEADING_OK", "MOVE_STRAIGHT_REQUIRED"),
                            ("fine_alignment", "FINE_ALIGNMENT_OK", "FINE_ALIGNMENT_REQUIRED")):
        if inp.get("nav_fresh") and flags.get(flag):
            health.add(bit)
        elif flag in required_flags:
            inhibit.add(wait)            # missing or not fresh: the output is not usable yet
    if not inp.get("ownership"):
        health.add("NO_DUPLICATE_PROCESS")

    unmet = []                                   # required conditions not (yet) good enough
    for name in inp.get("unknown_required") or []:
        # A typo in readiness.require must not quietly drop the condition: fail closed.
        inhibit.add("CONFIG_INVALID")
        unmet.append(f"readiness.require has an unknown condition '{name}'")
    for name, c in conds.items():
        if name not in CONDITIONS:
            continue
        hbit, ibit, label = CONDITIONS[name]
        if c.get("ok"):
            health.add(hbit)
        if c.get("required") and not c.get("ready"):
            inhibit.add(ibit)
            unmet.append(c.get("reason") or f"{label} not ready")

    def out(state: str, action: str, why: List[str]) -> Dict:
        if state != "READY" and not inhibit:
            inhibit.add("INTERNAL_FAULT")    # never "not ready" with an empty mask: the vehicle checks it
        for r in why:
            if r and r not in reasons:
                reasons.append(r)
        return {"state": state, "state_code": STATE_CODE[state],
                "requested_action": action, "requested_action_code": ACTION_CODE[action],
                "action_text": ACTION_TEXT[action],
                "health_flags": mask(health, HEALTH_BITS), "inhibit_flags": mask(inhibit, INHIBIT_BITS),
                "inhibit": sorted(inhibit, key=INHIBIT_BITS.get), "reasons": reasons}

    product_why = [r for r in (inp.get("product_reasons") or []) if r]

    if inp.get("ownership"):
        inhibit.add("OWNERSHIP_CONFLICT")
        return out("FAULT", "SERVICE_REQUIRED", [inp["ownership"]])
    if auto.get("state") == "gave_up" and not inp.get("nav_running"):
        inhibit.add("STARTUP_TIMEOUT")
        return out("FAULT", "SERVICE_REQUIRED", [auto.get("detail") or "AIO NAV did not start"] + product_why)
    # Automatic: the coordinator restarts AIO NAV by itself, so a failure asks the vehicle to stop
    # or hold still and says what is being done, instead of asking for service.
    automatic = (auto.get("enabled") and not inp.get("hold") and not inp.get("needs_service")
                 and auto.get("state") not in ("gave_up", "disabled"))
    auto_why = [auto["detail"]] if automatic and auto.get("detail") else []
    if inp.get("ever_ready") and not inp.get("nav_running") and (inp.get("wanted") or automatic) \
            and not inp.get("hold"):
        # READY was reached and AIO NAV is gone although it should run: the vehicle may be moving.
        return out("FAULT", "SAFE_STOP", ["AIO NAV stopped after it was ready"] + auto_why)
    if st == "UNKNOWN" or (inp.get("booting") and not inp.get("nav_running")):
        inhibit.add("BOOT_IN_PROGRESS")      # first: start-up runs BOOTING -> CHECKING -> ..., never back
        return out("BOOTING", "HOLD_STILL", ["System starting"])
    if (auto.get("enabled") and auto.get("state") in ("waiting", "retry_wait", "starting")
            and not inp.get("ever_ready") and not inp.get("nav_running")):
        # The coordinator is still working through its start-up plan (wait, start, retry).
        return out("CHECKING", "HOLD_STILL", [auto.get("detail") or "Waiting to start AIO NAV"] + unmet)
    if automatic and auto.get("state") == "starting" and not inp.get("nav_fresh"):
        inhibit.add("NAV_OUTPUT_STALE")      # being started by the coordinator: say so
        return out("NAV_STARTING", "HOLD_STILL", [auto.get("detail") or "Starting AIO NAV"])
    if st == "FAULT":
        if inp.get("nav_running") and not inp.get("ever_output"):
            inhibit.add("STARTUP_TIMEOUT")
        if automatic:                        # being restarted: nobody needs to do anything
            return out("FAULT", "SAFE_STOP" if inp.get("ever_ready") else "HOLD_STILL",
                       product_why[:1] + auto_why)
        # After READY the vehicle may be moving: stop using navigation first, then service.
        return out("FAULT", "SAFE_STOP" if inp.get("ever_ready") else "SERVICE_REQUIRED", product_why)
    if st == "STOPPED":
        if inp.get("hold"):
            inhibit.add("MAINTENANCE_HOLD")
            return out("MAINTENANCE", "MAINTENANCE_HOLD", ["Stopped by an operator"])
        if auto.get("enabled") and auto.get("state") not in ("done", "ready_this_boot", "disabled"):
            return out("CHECKING", "HOLD_STILL", [auto.get("detail") or "Waiting to start AIO NAV"] + unmet)
        # Stopped and nobody asked it to run (an operator Stop): maintenance, not a fault.
        return out("MAINTENANCE", "MAINTENANCE_HOLD", product_why or ["AIO NAV is not running"])
    if st == "STARTING":
        if not inp.get("nav_fresh"):
            inhibit.add("NAV_OUTPUT_STALE")  # no valid output yet
        return out("NAV_STARTING", "HOLD_STILL", product_why)
    if st == "INITIALIZING":
        step = (inp.get("step") or {}).get("key")
        phase, ibit, action = PHASE_OF_STEP.get(step, ("REFINING", "FINE_ALIGNMENT_REQUIRED", "KEEP_DRIVING"))
        inhibit.add(ibit)
        if inp.get("ever_ready"):            # alignment lost after READY: the filter restarted
            # (not the alignment prompt: "drive straight" would contradict the safe stop)
            return out("DEGRADED", "SAFE_STOP", ["Alignment lost after it was ready"])
        return out(phase, action, product_why + unmet)
    # Product READY: the filter is aligned; the required extra conditions decide. The product
    # state is debounced, so the latest packet may already disagree: READY only with no inhibit.
    if not inhibit:
        return out("READY", "NONE", [])
    if inp.get("ever_ready"):
        return out("DEGRADED", "SAFE_STOP", unmet or ["Navigation output no longer valid"])
    if not inp.get("nav_fresh"):
        return out("NAV_STARTING", "HOLD_STILL", ["Waiting for navigation output"])
    for bit, (phase, action) in (("KEEP_STILL_REQUIRED", ("KEEP_STILL", "HOLD_STILL")),
                                 ("MOVE_STRAIGHT_REQUIRED", ("MOVE_STRAIGHT", "DRIVE_STRAIGHT"))):
        if bit in inhibit:
            return out(phase, action, unmet)
    return out("REFINING", "KEEP_DRIVING", unmet)


class Condition:
    """Hysteresis for one READY condition: it must hold ``stable_s`` before it counts, and once
    counted it is dropped only after failing for ``loss_s`` (a single missed sample, or a 100 Hz
    topic between two listening windows, must not toggle the vehicle's authorisation)."""

    def __init__(self, stable_s: float, loss_s: float):
        self.stable_s, self.loss_s = stable_s, loss_s
        self.ok_since: Optional[float] = None
        self.bad_since: Optional[float] = None
        self.ready = False

    def update(self, ok: Optional[bool], now: float) -> bool:
        if ok:
            if self.ok_since is None:
                self.ok_since = now
            self.bad_since = None
        else:
            if self.bad_since is None:
                self.bad_since = now
            if not self.ready or now - self.bad_since >= self.loss_s:
                self.ok_since, self.ready = None, False
        if self.ok_since is not None and now - self.ok_since >= self.stable_s:
            self.ready = True
        return self.ready
