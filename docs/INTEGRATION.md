# AIO Navigation — Integration Guide

This guide is for the software that **receives the navigation output** of an AIO navigation
device, and for the people who connect it to a vehicle. Part A describes the navigation data;
Part B describes the readiness signal that tells a vehicle when it may use that data.

Contents:

**Part A: Navigation data**

1. [Outputs at a glance](#1-outputs-at-a-glance)
2. [UDP navigation packet (0x04)](#2-udp-navigation-packet-0x04)
3. [ROS 2 topics](#3-ros-2-topics)
4. [Log files](#4-log-files)

**Part B: Readiness for the vehicle (0x05)**

5. [When the vehicle may use the data](#5-when-the-vehicle-may-use-the-data)
6. [Status packet (0x05)](#6-status-packet-0x05)
7. [The reference receiver](#7-the-reference-receiver)
8. [Security of the link](#8-security-of-the-link)

---

# Part A: Navigation data

## 1. Outputs at a glance

| Output | For | Rate | Format |
|--------|-----|------|--------|
| **UDP 0x04** | Your application (the product interface) | 100 Hz by default | 156-byte binary packet ([section 2](#2-udp-navigation-packet-0x04)) |
| **ROS 2 topics** | Internal development | Output rate | Standard ROS messages ([section 3](#3-ros-2-topics)) |
| **Log files** | Analysis after the run | Output rate | Tab-separated text, downloaded from the user pages ([section 4](#4-log-files)) |

The navigation filter, **AIO NAV**, produces all three. The status packet of Part B is added by the
device's dashboard.

## 2. UDP navigation packet (0x04)

### 2.1 Overview

| Item | Value |
|------|-------|
| Transport | UDP over IPv4, one packet per epoch, no acknowledgement |
| Rate | `output_rate` in `aio_nav.yaml` (default 100 Hz) |
| Packet | 156 bytes, binary, little-endian |
| Destinations | `127.0.0.1:9000` on the device (the dashboard) **and** one external `host:port` (`output_udp` in `aio_nav.yaml`) |
| Starts | As soon as AIO NAV runs, **before** alignment is complete: see [section 2.5](#25-status-flags) and Part B for when the data is valid |

Each destination gets its own copy of every packet, so your receiver and the dashboard do not
compete for data. When the device's Wi-Fi hotspot is on, the dashboard also sends a copy of every
packet to each phone or tablet on the hotspot, on UDP port 9000. Do not run another listener on
`127.0.0.1:9000` on the device: only one program can hold it, and the dashboard needs it.

### 2.2 Setting the destination

1. On the Maintenance view → **AIO NAV config**, set **UDP destination** to your computer's IP and
   port, for example `192.0.2.50:9000`. Set it in the file of each mode you use: `aio_nav.yaml`
   (Live) and `aio_nav_bag.yaml` (Bag replay). In the file itself:
   ```yaml
   output_udp: "192.0.2.50:9000"     # your computer's IP and port
   ```
2. Restart AIO NAV (**Save and restart AIO NAV** on that page, or **Restart** on the Overview).
3. On the receiving computer, allow incoming UDP on that port in the firewall.

Only **one** external destination is supported, and it must be a unicast IPv4 address. To feed
several applications, receive on one computer and forward from there.

### 2.3 Packet layout

| Bytes | Field | Type | Value |
|-------|-------|------|-------|
| 0–1 | Sync | 2 × uint8 | `0x55 0xAA` |
| 2 | Message ID | uint8 | `0x04` (navigation solution) |
| 3–4 | Payload length | uint16 | `150` |
| 5–154 | Payload | 150 bytes | [Section 2.4](#24-payload-fields) |
| 155 | Checksum | uint8 | Sum of bytes 2–154, modulo 256 |

Discard any packet whose sync, ID, length or checksum does not match. Other message IDs may arrive
on the same port (the 0x05 status packet of Part B): dispatch on the ID and the length.

### 2.4 Payload fields

Offsets are from the start of the packet (the payload starts at byte 5). Python `struct` format of
the payload: `<Qdd28fH3f`.

| Offset | Field | Type | Unit | Notes |
|-------:|-------|------|------|-------|
| 5 | time | uint64 | µs | GPS time of week × 10⁶ (resets each week). In bag replay, follows the replay clock. |
| 13 | latitude | float64 | deg | WGS84 |
| 21 | longitude | float64 | deg | WGS84 |
| 29 | height | float32 | m | Height of AIO NAV's position solution |
| 33 | velocity north | float32 | m/s | |
| 37 | velocity east | float32 | m/s | |
| 41 | velocity up | float32 | m/s | **Up** is positive |
| 45 | roll | float32 | deg | |
| 49 | pitch | float32 | deg | |
| 53 | heading | float32 | deg | 0 = north, 90 = east (clockwise). May be sent as −180…180; add 360 to negative values for 0…360. |
| 57 / 61 / 65 | position σ north / east / down | float32 | m | 1-sigma accuracy |
| 69 / 73 / 77 | velocity σ north / east / down | float32 | m/s | 1-sigma accuracy |
| 81 / 85 / 89 | roll σ / pitch σ / heading σ | float32 | deg | 1-sigma accuracy |
| 93 / 97 / 101 | gyro bias x / y / z | float32 | deg/h | Estimated IMU errors |
| 105 / 109 / 113 | accelerometer bias x / y / z | float32 | mg | |
| 117 / 121 / 125 | gyro scale factor x / y / z | float32 | ppm | |
| 129 / 133 / 137 | accelerometer scale factor x / y / z | float32 | ppm | |
| 141 | flags | uint16 | — | Status bits, [section 2.5](#25-status-flags) |
| 143 | odometer scale | float32 | — | Used when odometry aiding is configured |
| 147 | mount pitch | float32 | deg | Estimated sensor mounting angles |
| 151 | mount yaw | float32 | deg | |

### 2.5 Status flags

Only the low byte of `flags` is used, and it is sent **bit-reversed**. Reverse the 8 bits first,
then read:

| Bit (after reversing) | Bit on the wire | Name | Meaning when 1 |
|----:|----:|------|----------------|
| 0 | 7 | ZUPT | Zero-velocity update applied (vehicle stationary) |
| 1 | 6 | ZIHR | Zero heading-rate update applied (heading not changing while stationary) |
| 2 | 5 | NHC | Non-holonomic constraint applied (no sideways or vertical slip) |
| 3 | 4 | GNSS | GNSS measurement applied in this epoch |
| 4 | 3 | VUPT | Vehicle velocity update from odometry applied |
| 5 | 2 | ALIGN | Coarse alignment complete |
| 6 | 1 | HEADING | Initial heading valid |
| 7 | 0 | FINE | Fine alignment complete |

**When is the data valid?** ALIGN, HEADING and FINE all 1 is the minimum for the solution to be
meaningful. It is not the whole answer: the device may need more before it calls the output Ready
(for example RTK where RTK is available). A vehicle should use the status packet of Part B. ZUPT,
ZIHR, NHC, GNSS and VUPT change from epoch to epoch and only report which aiding was used; GNSS
updates arrive at the GNSS rate, so the GNSS bit is set only in some packets.

### 2.6 Receiver example (Python 3, no extra packages)

```python
import socket
import struct

PORT = 9000                                   # the port in output_udp
PAYLOAD = struct.Struct("<Qdd28fH3f")         # 150 bytes, little-endian
FLAG_NAMES = ["ZUPT", "ZIHR", "NHC", "GNSS", "VUPT", "ALIGN", "HEADING", "FINE"]

def reverse_bits8(x):
    return int(f"{x & 0xFF:08b}"[::-1], 2)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", PORT))
while True:
    pkt, _ = sock.recvfrom(2048)
    if len(pkt) != 156 or pkt[:3] != b"\x55\xaa\x04" or pkt[155] != sum(pkt[2:155]) & 0xFF:
        continue                              # not a navigation packet (or a 0x05 status packet)
    f = PAYLOAD.unpack_from(pkt, 5)
    flags = reverse_bits8(f[31])
    active = [n for i, n in enumerate(FLAG_NAMES) if flags >> i & 1]
    print(f"t={f[0] / 1e6:.3f} lat={f[1]:.8f} lon={f[2]:.8f} h={f[3]:.2f} "
          f"vn={f[4]:.2f} ve={f[5]:.2f} vu={f[6]:.2f} hdg={f[9]:.1f} {' '.join(active)}")
```

For C/C++, use a packed struct (`#pragma pack(push, 1)`) with the field order of section 2.4:
`uint64_t time_us; double lat, lon; float alt, vn, ve, vu, roll, pitch, heading, 9 × σ,
12 × IMU errors; uint16_t flags; float odo_scale, mount_pitch, mount_yaw;` (150 bytes).

### 2.7 Checking the integration

1. On the device's user pages, the **Navigation output** line (Overview or Navigation page) shows
   the rate and your computer's address, for example *100 Hz to 192.0.2.50:9000*.
2. Your receiver prints packets at the expected rate (100 per second by default).
3. Latitude, longitude and heading match the Navigation page.
4. The flags show ALIGN, HEADING and FINE when the user pages show **Ready**.

If the device shows a rate and your address but your application receives nothing, check the
address and the firewall of the receiving computer.

## 3. ROS 2 topics

AIO NAV also publishes on ROS 2. These topics are for internal development; the UDP output is the
product interface. They are published **only after coarse alignment**.

| Topic | Type | Content |
|-------|------|---------|
| `/nav/fix` | `sensor_msgs/NavSatFix` | WGS84 position; `status.service` carries the flags (not bit-reversed) |
| `/nav/odometry` | `nav_msgs/Odometry` | TWD97 position, ENU velocity, covariances |
| `/nav/path` | `nav_msgs/Path` | Recent track |
| TF `map` → `imu_link` | — | Pose in the TWD97 map frame |

The ROS domain must match AIO NAV's: **Live** uses domain 10 (this computer only), **Bag replay**
uses domain 13 (network).

## 4. Log files

When enabled in `aio_nav.yaml` (`save_fusion_txt`, `save_parsed_txt`), AIO NAV writes tab-separated
text logs once alignment is complete, into its log folder (the folder of `fusion_txt_path`, or
`output/` next to aio-nav-ros). Users download them from the **Data** page.

| File | Content |
|------|---------|
| `AIO-NAV.txt` (the name set by `fusion_txt_path`) | The navigation solution at the output rate: time, position, velocity, attitude, accuracy, IMU errors, flags |
| `AIO-NAV_imu.txt`, `AIO-NAV_gnss.txt`, `AIO-NAV_odom.txt` | The sensor inputs, parsed (`save_parsed_txt`) |

> **To be added:** the column layout of each file. It is defined by aio-nav-ros; until it is
> written here, ask the navigation team.

# Part B: Readiness for the vehicle (0x05)

## 5. When the vehicle may use the data

The 0x04 packet starts **before** alignment is complete, so receiving it does not mean the data may
be used. The device therefore also sends a **status packet (0x05)**: its own verdict, the same one
its pages show. The rule for a vehicle is:

> Use the navigation data only while the newest status packet, at most 3 s old, says **READY** with
> **`inhibit_flags` = 0**, and has said so for 2 s, in the same session; and the newest 0x04 is at
> most 0.5 s old, with the ALIGN, HEADING and FINE flags set.

[Section 7](#7-the-reference-receiver) gives the full rules and a reference implementation.

The status packet keeps coming after READY: **silence means "do not use"**. It goes, twice a second
and at once on every change, to the **same address and port as 0x04** (`status_udp`, on by default;
[Configuration Reference](CONFIG.md)). The same snapshot is available as JSON at
`http://<device-ip>:8080/api/readiness`.

> **Open question:** the status packet is sent also when no vehicle is connected and during bag
> replay; then it reflects the replayed data. Whether it should then say MAINTENANCE, or stop, is not
> decided yet. Do not connect a vehicle to a device that replays bags.

## 6. Status packet (0x05)

### 6.1 Receiving both packets on one port

UDP lets any number of senders send to one port; only the vehicle binds it. The vehicle keeps its
one socket and its `55 AA` frame parser, and dispatches on the message ID (byte 2): `0x04` =
navigation (156 bytes), `0x05` = status (42 bytes). A parser must check the ID and the length and
skip any other frame; one that read every frame as 0x04 would misread the status packet. To receive
the status packet on a separate socket instead, set `status_udp.destination` to that address and
port. AIO NAV itself only sends 0x04; 0x05 is reserved for this packet.

### 6.2 Frame and fields

The frame is the same as 0x04: `55 AA`, message ID `0x05`, payload length `36` (uint16 LE),
payload, checksum (sum of the bytes from the message ID to the end of the payload, modulo 256).
42 bytes in all, little-endian. Python `struct` of the payload: `<BBBBIIQIIBBHHH`.

| Offset (payload) | Field | Type | Meaning |
|---:|---|---|---|
| 0 | protocol_version | uint8 | `1` |
| 1 | state | uint8 | [Section 6.3](#63-states) |
| 2 | requested_action | uint8 | [Section 6.4](#64-requested-actions) |
| 3 | gnss_quality | uint8 | 0 unknown, 1 none, 2 SPP, 3 RTK float, 4 RTK fix |
| 4 | session_id | uint32 | New at every dashboard start |
| 8 | sequence | uint32 | +1 per packet, wraps to 0 |
| 12 | uptime_ms | uint64 | Dashboard uptime |
| 20 | health_flags | uint32 | Conditions that hold ([section 6.5](#65-health-and-inhibit-bits)) |
| 24 | inhibit_flags | uint32 | Reasons not to use 0x04; **any bit set = do not use** |
| 28 | nav_flags | uint8 | The 0x04 flags in normal bit order (**not** bit-reversed); 0 while the 0x04 output is not fresh |
| 29 | reserved | uint8 | 0 |
| 30 | nav_rate_dhz | uint16 | Navigation output rate × 10 |
| 32 | nav_age_ms | uint16 | Age of the last 0x04; 65535 = none or older |
| 34 | reserved | uint16 | 0 |

### 6.3 States

| Code | State | Meaning |
|---:|---|---|
| 0 | `BOOTING` | Dashboard just started |
| 1 | `CHECKING` | Waiting for prerequisites or for the automatic start |
| 2 | `NAV_STARTING` | AIO NAV started, no valid output yet |
| 3 | `KEEP_STILL` | Alignment stage 1 (output flows, not aligned) |
| 4 | `MOVE_STRAIGHT` | Alignment stage 2 (no valid heading yet) |
| 5 | `REFINING` | Alignment stage 3, or aligned while a required condition is missing |
| 6 | `READY` | May be used (`inhibit_flags` is 0) |
| 7 | `DEGRADED` | Was READY in this run; a required condition or the alignment is lost |
| 8 | `FAULT` | AIO NAV, its output or its ownership has a problem |
| 9 | `MAINTENANCE` | Stopped (operator Stop, or not started) |

### 6.4 Requested actions

What the device asks the operator (or the vehicle) to do now.

| Code | Action | Text |
|---:|---|---|
| 0 | `NONE` | Nothing to do. |
| 1 | `HOLD_STILL` | Keep the vehicle completely still. |
| 2 | `DRIVE_STRAIGHT` | Drive straight at a steady speed in a safe, open area. |
| 3 | `KEEP_DRIVING` | Keep driving normally with GNSS available. |
| 4 | `SAFE_STOP` | Stop using navigation and follow the vehicle's safety procedure. |
| 5 | `SERVICE_REQUIRED` | Maintenance is needed. |
| 6 | `MAINTENANCE_HOLD` | Stopped. Press Resume or Start on the Overview page. |

### 6.5 Health and inhibit bits

| Bit | health_flags | inhibit_flags |
|---:|---|---|
| 0 | `DASHBOARD_OK` | `BOOT_IN_PROGRESS` |
| 1 | `CONFIG_OK` | `CONFIG_INVALID` |
| 2 | `IMU_STREAM_OK` | `IMU_MISSING_OR_STALE` |
| 3 | `GNSS_STREAM_OK` | `GNSS_MISSING_OR_STALE` |
| 4 | `GNSS_QUALITY_OK` | `GNSS_QUALITY_LOW` |
| 5 | `NAV_PROCESS_OK` | `NAV_NOT_RUNNING` |
| 6 | `NAV_OUTPUT_OK` | `NAV_OUTPUT_STALE` |
| 7 | `ALIGNMENT_OK` | `KEEP_STILL_REQUIRED` |
| 8 | `HEADING_OK` | `MOVE_STRAIGHT_REQUIRED` |
| 9 | `FINE_ALIGNMENT_OK` | `FINE_ALIGNMENT_REQUIRED` |
| 10 | `DSO_OK` | `DSO_NOT_READY` |
| 11 | `EXTERNAL_ROUTE_OK` | `EXTERNAL_ROUTE_DOWN` |
| 12 | `RTK_CORRECTION_OK` | `RTK_CORRECTION_LOST` |
| 13 | `STORAGE_OK` | `STORAGE_NOT_WRITABLE` |
| 14 | `VEHICLE_ACK_OK` | `MAINTENANCE_HOLD` |
| 15 | `TIME_SYNC_OK` | `STARTUP_TIMEOUT` |
| 16 | `NO_DUPLICATE_PROCESS` | `OWNERSHIP_CONFLICT` |
| 17 | `WIFI_OK` | `INTERNAL_FAULT` |

Bits that are not implemented stay 0 (for example `VEHICLE_ACK_OK`, `RTK_CORRECTION_OK`,
`TIME_SYNC_OK`, `WIFI_OK`). Receivers ignore unknown bits. Every state other than READY has at least
one inhibit bit set. The numbers are a contract: new codes and bits are appended, never renumbered.

### 6.6 Golden vector

Any implementation must encode and decode exactly this packet: READY, action NONE, RTK float,
session `0x12345678`, sequence 482, uptime 123456789 ms, health `0x10FFF`, inhibit 0, nav flags
`0xE8`, 100.0 Hz, age 12 ms:

```
55aa0524000106000378563412e201000015cd5b0700000000ff0f010000000000e800e8030c0000005c
```

## 7. The reference receiver

The reference implementation of the receiver's rules is `aio_system_dashboard/vehicle/gate.py` in
this repository. A vehicle's own software decides; this is what it can test against.

1. **Start blocked.** Block at once when no valid 0x05 arrived for 3 s, on a bad checksum or an
   unknown version, and when `session_id` changes (the dashboard restarted).
2. **Allow** only when `state` is READY (6) **and** `inhibit_flags` is 0, continuously for 2 s in the
   same session.
3. **Check the 0x04 itself**: at most 0.5 s old, the ALIGN, HEADING and FINE flags set, a time stamp
   that moves forward, and only finite values.
4. **Losses and a second sender.** A lost status packet alone never blocks (the sequence number
   counts losses); only the time-outs do. A status packet far behind the newest one in the same
   session (more than 5 sequence numbers) means a second sender: block at once and start over.

To watch the rules work where the vehicle listens, from a checkout of this repository (the release
package does not include `tools/`):

```bash
python3 tools/vehicle_gate.py --port 9000
```

0x04 and 0x05 both arrive on 9000; use `--status-port` if 0x05 goes to another port, and
`--from <device-ip>` to accept packets from that sender only.

## 8. Security of the link

The packets carry no authentication: anything that can send to the vehicle's port can send a READY
packet. Therefore:

- keep the link between the device and the vehicle private (a direct cable or a closed VLAN);
- accept packets only from the device's address (`--from` in the reference receiver);
- the device's hotspot is a closed network: devices on it receive copies of the packets but cannot
  reach the vehicle's network through the device.
