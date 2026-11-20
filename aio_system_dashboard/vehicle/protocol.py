"""The numbers of the 0x05 status packet: state and action codes, bit positions, GNSS quality codes.

The core (state/readiness.py) works by name only. This table turns a readiness snapshot into the
fields the vehicle reads. The numbers are a contract with the vehicle (docs/INTEGRATION.md, "Status
packet", and the vehicle-side aio-nav-receiver): append, never renumber.
"""

from typing import Dict, Iterable, List

STATE_CODE = {
    "BOOTING": 0, "CHECKING": 1, "NAV_STARTING": 2, "KEEP_STILL": 3, "MOVE_STRAIGHT": 4,
    "REFINING": 5, "READY": 6, "DEGRADED": 7, "FAULT": 8, "MAINTENANCE": 9,
}
ACTION_CODE = {
    "NONE": 0, "HOLD_STILL": 1, "DRIVE_STRAIGHT": 2, "KEEP_DRIVING": 3, "SAFE_STOP": 4,
    "SERVICE_REQUIRED": 5, "MAINTENANCE_HOLD": 6,
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
GNSS_QUALITY_CODE = {"unknown": 0, "none": 1, "spp": 2, "rtk_float": 3, "rtk_fix": 4}

STATES = tuple(sorted(STATE_CODE, key=STATE_CODE.get))      # by code, for printing
ACTIONS = tuple(sorted(ACTION_CODE, key=ACTION_CODE.get))


def mask(names: Iterable[str], table: Dict[str, int]) -> int:
    out = 0
    for n in names:
        out |= 1 << table[n]
    return out


def names_of(value: int, table: Dict[str, int]) -> List[str]:
    return [n for n, b in sorted(table.items(), key=lambda kv: kv[1]) if value >> b & 1]


def wire_fields(snap: Dict) -> Dict:
    """The numbers of a readiness snapshot: state_code, requested_action_code, health_flags,
    inhibit_flags, gnss_quality_code."""
    return {"state_code": STATE_CODE[snap["state"]],
            "requested_action_code": ACTION_CODE[snap["requested_action"]],
            "health_flags": mask(snap.get("health") or (), HEALTH_BITS),
            "inhibit_flags": mask(snap.get("inhibit") or (), INHIBIT_BITS),
            "gnss_quality_code": GNSS_QUALITY_CODE.get(snap.get("gnss_quality") or "unknown", 0)}
