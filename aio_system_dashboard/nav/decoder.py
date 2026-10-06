"""AIO NAV binary packet (msg_id 0x04) decoder.

Derived from setup_env ``decoder.py:parse_nav_solution_packet`` with two fixes
for the aio-nav-ros wire format (see ``nav_solution_udp_writer.cpp``):

* Flag bit 3 (after bit reversal) is VUPT / odometry update, not ``imu_valid``.
* The third velocity component is velocity **Up** (``-vel_d``), so it is exposed
  as ``velocity_up``.

Packet layout::

    55 AA | msg_id(0x04) | payload_len (<H) | payload (<Qdd28fH3f) | checksum

The checksum is the 8-bit sum of bytes from msg_id through the end of payload.
The flags byte is bit-reversed on the wire.
"""

import struct
from typing import Dict, Optional

NAV_SYNC = b"\x55\xAA"
NAV_MSG_ID_SOLUTION = 0x04
NAV_HEADER_SIZE = 5  # sync(2) + msg_id(1) + payload_len(2)
NAV_PAYLOAD_STRUCT = "<Qdd" + "f" * 28 + "H" + "fff"
NAV_PAYLOAD_SIZE = struct.calcsize(NAV_PAYLOAD_STRUCT)
NAV_PACKET_SIZE = NAV_HEADER_SIZE + NAV_PAYLOAD_SIZE + 1

# Navigation_Output_t.flags bits (C side, i.e. after undoing the wire bit reversal).
FLAG_BITS = (
    ("zupt", 0),
    ("zihr", 1),
    ("nhc", 2),
    ("gnss", 3),
    ("vupt", 4),
    ("alignment", 5),
    ("heading_valid", 6),
    ("fine_alignment", 7),
)
FLAG_NAMES = tuple(name for name, _ in FLAG_BITS)

_PAYLOAD_NAMES = (
    "time_us",
    "latitude",
    "longitude",
    "height",
    "velocity_north",
    "velocity_east",
    "velocity_up",
    "roll",
    "pitch",
    "heading",
    "position_north_std",
    "position_east_std",
    "height_std",
    "velocity_north_std",
    "velocity_east_std",
    "velocity_up_std",
    "roll_std",
    "pitch_std",
    "heading_std",
    "gyro_bias_x",
    "gyro_bias_y",
    "gyro_bias_z",
    "accel_bias_x",
    "accel_bias_y",
    "accel_bias_z",
    "gyro_scale_x",
    "gyro_scale_y",
    "gyro_scale_z",
    "accel_scale_x",
    "accel_scale_y",
    "accel_scale_z",
    "flags_wire",
    "odo_scale",
    "mount_pitch",
    "mount_yaw",
)


def reverse_bits8(x: int) -> int:
    r = 0
    for i in range(8):
        r |= ((x >> i) & 1) << (7 - i)
    return r


def decode_flags(flags_c: int) -> Dict[str, bool]:
    """Decode C-side flag bits (already un-reversed) into named booleans."""
    return {name: bool(flags_c & (1 << bit)) for name, bit in FLAG_BITS}


def _checksum(body: bytes) -> int:
    return sum(body) & 0xFF


def parse_nav_packet(data: bytes) -> Optional[Dict]:
    """Parse one NAV solution datagram. Returns None if it is not a valid packet."""
    if len(data) < NAV_PACKET_SIZE:
        return None
    if data[0:2] != NAV_SYNC or data[2] != NAV_MSG_ID_SOLUTION:
        return None
    payload_len = struct.unpack_from("<H", data, 3)[0]
    if payload_len != NAV_PAYLOAD_SIZE:
        return None
    end = NAV_HEADER_SIZE + payload_len
    if data[end] != _checksum(data[2:end]):
        return None
    fields = struct.unpack_from(NAV_PAYLOAD_STRUCT, data, NAV_HEADER_SIZE)

    out = dict(zip(_PAYLOAD_NAMES, fields))
    out["time_s"] = out.pop("time_us") / 1e6
    flags_c = reverse_bits8(int(out["flags_wire"]) & 0xFF)
    out["flags_c"] = flags_c
    out.update(decode_flags(flags_c))
    return out


def encode_nav_packet(
    *,
    time_s: float,
    latitude: float,
    longitude: float,
    height: float = 0.0,
    velocity_north: float = 0.0,
    velocity_east: float = 0.0,
    velocity_up: float = 0.0,
    roll: float = 0.0,
    pitch: float = 0.0,
    heading: float = 0.0,
    flags_c: int = 0,
    stds=(0.0,) * 9,
    imu_cal=(0.0,) * 12,
    odo_scale: float = 1.0,
    mount_pitch: float = 0.0,
    mount_yaw: float = 0.0,
) -> bytes:
    """Build a NAV packet exactly as aio_nav_node does (used by fake sender and tests)."""
    payload = struct.pack(
        NAV_PAYLOAD_STRUCT,
        int(round(time_s * 1e6)),
        latitude,
        longitude,
        height,
        velocity_north,
        velocity_east,
        velocity_up,
        roll,
        pitch,
        heading,
        *stds,
        *imu_cal,
        reverse_bits8(flags_c & 0xFF),
        odo_scale,
        mount_pitch,
        mount_yaw,
    )
    head = NAV_SYNC + bytes([NAV_MSG_ID_SOLUTION]) + struct.pack("<H", len(payload))
    return head + payload + bytes([_checksum(head[2:] + payload)])


def flags_to_int(**flags: bool) -> int:
    """Compose C-side flag bits from names, e.g. flags_to_int(alignment=True, gnss=True)."""
    value = 0
    for name, bit in FLAG_BITS:
        if flags.get(name):
            value |= 1 << bit
    return value
