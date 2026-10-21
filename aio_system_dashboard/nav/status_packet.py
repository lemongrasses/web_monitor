"""0x05 status packet: the readiness snapshot for the vehicle, next to the 0x04 navigation data.

Same frame as 0x04 (nav/decoder.py)::

    55 AA | msg_id (0x05) | payload_len u16 LE (36) | payload | checksum u8

checksum = 8-bit sum of the bytes from msg_id to the end of the payload. 42 bytes in all.

Payload (little-endian, fixed 36 bytes)::

    off size field              type  meaning
      0   1  protocol_version   u8    1
      1   1  state              u8    readiness.STATES index (6 = READY)
      2   1  requested_action   u8    readiness.ACTIONS index
      3   1  gnss_quality       u8    0 unknown, 1 none, 2 SPP, 3 RTK float, 4 RTK fix
      4   4  session_id         u32   new at every dashboard start
      8   4  sequence           u32   +1 per packet, wraps to 0
     12   8  uptime_ms          u64   dashboard monotonic uptime
     20   4  health_flags       u32   readiness.HEALTH_BITS
     24   4  inhibit_flags      u32   readiness.INHIBIT_BITS; any bit set = do not use 0x04
     28   1  nav_flags          u8    0x04 flags, C-side bit order (NOT bit-reversed like 0x04)
     29   1  reserved           u8    0
     30   2  nav_rate_dhz       u16   navigation output rate x 10
     32   2  nav_age_ms         u16   age of the last 0x04, clamped to 65535 (65535 also = none)
     34   2  reserved           u16   0

0x05 is reserved for this packet (AIO NAV itself only sends 0x04). By default it goes to the
same address and port as 0x04; the receiver tells them apart by msg_id (and length: 0x04 is 156
bytes, 0x05 42). status_udp.message_id stays configurable.
"""

import struct
from typing import Dict, Optional

SYNC = b"\x55\xAA"
DEFAULT_MESSAGE_ID = 0x05
PROTOCOL_VERSION = 1
PAYLOAD = struct.Struct("<BBBBIIQIIBBHHH")
PAYLOAD_SIZE = PAYLOAD.size              # 36
PACKET_SIZE = 5 + PAYLOAD_SIZE + 1       # 42
FIELDS = ("protocol_version", "state", "requested_action", "gnss_quality", "session_id", "sequence",
          "uptime_ms", "health_flags", "inhibit_flags", "nav_flags", "reserved0", "nav_rate_dhz",
          "nav_age_ms", "reserved1")


def _clamp(v, hi: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return hi
    return min(max(v, 0), hi)


def encode(f: Dict, message_id: int = DEFAULT_MESSAGE_ID) -> bytes:
    """Build one packet from a dict of the payload fields (missing reserved fields are 0)."""
    age = f.get("nav_age_ms")
    payload = PAYLOAD.pack(
        PROTOCOL_VERSION, _clamp(f["state"], 255), _clamp(f["requested_action"], 255),
        _clamp(f.get("gnss_quality", 0), 255), _clamp(f["session_id"], 0xFFFFFFFF),
        _clamp(f["sequence"], 0xFFFFFFFF), _clamp(f.get("uptime_ms", 0), 0xFFFFFFFFFFFFFFFF),
        _clamp(f["health_flags"], 0xFFFFFFFF), _clamp(f["inhibit_flags"], 0xFFFFFFFF),
        _clamp(f.get("nav_flags", 0), 255), 0, _clamp(f.get("nav_rate_dhz", 0), 0xFFFF),
        0xFFFF if age is None else _clamp(age, 0xFFFF), 0)
    head = SYNC + bytes([message_id & 0xFF]) + struct.pack("<H", PAYLOAD_SIZE)
    return head + payload + bytes([sum(head[2:] + payload) & 0xFF])


def decode(data: bytes, message_id: int = DEFAULT_MESSAGE_ID) -> Optional[Dict]:
    """Parse one packet; None if it is not a valid status packet of a supported version."""
    if len(data) < PACKET_SIZE or data[:2] != SYNC or data[2] != message_id:
        return None
    if struct.unpack_from("<H", data, 3)[0] != PAYLOAD_SIZE:
        return None
    if data[PACKET_SIZE - 1] != sum(data[2:PACKET_SIZE - 1]) & 0xFF:
        return None
    out = dict(zip(FIELDS, PAYLOAD.unpack_from(data, 5)))
    if out["protocol_version"] != PROTOCOL_VERSION:
        return None
    return out


def from_snapshot(snap: Dict, sequence: int) -> Dict:
    """The packet fields of a readiness snapshot (state/health.py)."""
    nav = snap.get("nav") or {}
    return {"state": snap["state_code"], "requested_action": snap["requested_action_code"],
            "gnss_quality": snap.get("gnss_quality_code", 0), "session_id": snap["session_id"],
            "sequence": sequence, "uptime_ms": snap.get("uptime_ms", 0),
            "health_flags": snap["health_flags"], "inhibit_flags": snap["inhibit_flags"],
            "nav_flags": nav.get("flags_c", 0), "nav_rate_dhz": round((nav.get("rate_hz") or 0.0) * 10),
            "nav_age_ms": nav.get("age_ms")}
