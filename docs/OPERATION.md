# AIO Navigation System — Operation Manual

[繁體中文版](OPERATION.zh-TW.md) · Settings: [Configuration manual](CONFIG.md)

This manual is for the people who **operate** the AIO navigation system (start it, align
it, watch it, collect data) and the people who **integrate** its navigation output into
their own software. Installation and settings are covered in the
[configuration manual](CONFIG.md).

Contents:

1. [What the system provides](#1-what-the-system-provides)
2. [Opening the dashboard](#2-opening-the-dashboard)
3. [Reading the dashboard](#3-reading-the-dashboard)
4. [Daily operation](#4-daily-operation)
5. [Alignment SOP](#5-alignment-sop)
6. [Navigation output over UDP (data integration)](#6-navigation-output-over-udp-data-integration)
7. [Other outputs: ROS 2 topics and log files](#7-other-outputs-ros-2-topics-and-log-files)
8. [Maintenance view (engineers)](#8-maintenance-view-engineers)
9. [Troubleshooting](#9-troubleshooting)
10. [Glossary](#10-glossary)

---

## 1. What the system provides

The device runs **AIO NAV**, an inertial navigation filter (IMU + GNSS, optionally
odometry). It produces a navigation solution (position, velocity, attitude) at 100 Hz.
The **web dashboard** on the same device lets you start the filter, align it, watch its
state, and download its logs.

| Service | Where | For | What it does |
|---------|-------|-----|--------------|
| **Navigation output** | UDP, binary, 100 Hz | Your application | The product output: position, velocity, attitude, accuracy and status flags (section 6). |
| **Product view** | `http://<device-ip>:8080` | Operators | Overview, Navigation and Data pages: is navigation usable, where is the vehicle, is the output streaming. |
| **AIO NAV control** | Overview page | Operators | Start, stop and restart the navigation filter (if enabled on this device). |
| **Data download** | Product view, Data page | Operators | Download AIO NAV log files to your computer. |
| **Maintenance view** | `http://<device-ip>:8081` | Engineers | Pinpoint whether a problem is in the system, a service, ROS, a sensor or the network; live camera and LiDAR view; driver diagnostics and restarts. |
| **`aio-dashboard` command** | Device terminal | Engineers | Start, stop, check and configure the dashboard itself. |

The dashboard only **monitors** the system. Navigation keeps running even if the
dashboard stops or a browser is closed.

## 2. Opening the dashboard

1. Connect your computer to the same network as the device (usually a direct Ethernet
   cable or the sensor switch).
2. Open a browser (Chrome, Edge or Firefox) at `http://<device-ip>:8080`.
   On the device itself, `aio-dashboard urls` prints the addresses.
3. If the page does not open, or shows **403 Forbidden**, this device only accepts
   specific computers (`access.allowed_clients` in the settings). Ask the administrator
   to add your computer's IP address.

The map background needs Internet access on **your** computer. Without it, the vehicle
position and track are still shown on a blank background.

## 3. Reading the dashboard

### 3.1 The state strip

A colored strip across the top of every page answers one question: *can I use the
navigation output right now?*

| State | Color | Meaning | What to do |
|-------|-------|---------|------------|
| **Ready** | Green | AIO NAV is running, data is fresh, and alignment is complete. The output can be used. | Operate normally. |
| **Initializing** | Amber | AIO NAV is running, but alignment is not finished. The reason is shown next to it (for example "Waiting for fine alignment"). | Follow the [Alignment SOP](#5-alignment-sop). Do not use the output yet. |
| **Fault** | Red | AIO NAV is not running, or no navigation data has arrived for 2 seconds. The reason is shown next to it. | See [Troubleshooting](#9-troubleshooting). |
| **Unknown** | Grey | The dashboard just started, or your browser lost its connection to the device. | Wait a few seconds; check the network if it persists. |

A state only changes after the condition has lasted about a second, and only returns to
Ready after about 2 stable seconds. A single lost packet never makes it flicker.

### 3.2 Status lamps

The lamps under the map follow the cockpit convention: **quiet when normal, lit when
something needs attention.**

| Lamp look | Meaning |
|-----------|---------|
| Pale green | Normal / done |
| Amber, fully lit | Needs attention or still in progress |
| Red, fully lit | Fault |
| Cyan, fully lit | An aiding function is active right now (Navigation page) |
| Grey | No data, idle, or not set up |

| Lamp | Shows |
|------|-------|
| **Alignment** | Coarse alignment (leveling) is done. |
| **Initial heading** | The filter has a valid heading. |
| **Fine alignment** | Fine alignment is done: heading accuracy reached its target. |
| **GNSS** | GNSS updates are arriving. "Unavailable" is a warning only: inertial navigation continues, but accuracy slowly degrades. |
| **Camera / LiDAR** | The sensor answers on the network. "Not set up" means no IP is configured; this is normal if not needed. |
| **Network** | The sensor network link is up. |
| **ZUPT / ZIHR / NHC / VUPT** (Navigation page) | Which aiding the filter is using at the moment (see the [glossary](#10-glossary)). Cyan = active, grey = idle. Idle is normal. |

GNSS, camera, LiDAR and network problems are shown as **advisories** (amber bars under
the lamps). They never change the state from Ready to Fault, because the navigation
solution is still usable.

### 3.3 UDP output line

| Label | Meaning |
|-------|---------|
| **Streaming** | The device is sending navigation packets at the expected rate. |
| **Low rate** | Packets arrive, but at under 80% of the expected rate. |
| **Stale** | No packet for more than 0.5 s. |
| **Lost** | No packet for more than 2 s. The state becomes Fault. |

**Sending to** shows where the output goes (section 6). UDP has no acknowledgement:
"Streaming" confirms that the device is sending, not that your application received it.

### 3.4 Pages

- **Overview**: state, AIO NAV control, UDP output, map with the last 60 s of track,
  position, heading (with an arrow; up = north), speed, roll and pitch, lamps,
  advisories, storage.
- **Navigation**: large map with the whole track of this run, and every value with its
  accuracy (±): position, velocity north/east/up, speed, attitude, NAV time. Aiding lamps
  and output details (destination, packet count) are at the bottom.
  **Show whole trajectory** zooms to the full track; **Follow vehicle** re-centers.
- **Data**: AIO NAV log folders. Click a folder to open it and **Download** to save a
  file. Nothing on the device can be changed from this page.

## 4. Daily operation

### 4.1 Before starting

- [ ] The device is powered and the GNSS antenna has a clear view of the sky.
- [ ] The vehicle is parked in its starting position and will stay **completely still**
      for the first part of alignment.
- [ ] Your computer opens the product view (section 2).
- [ ] The state strip is not Unknown (the dashboard is connected).

### 4.2 Starting AIO NAV

On the **Overview** page, the **AIO NAV** control line shows the filter's service state.

1. Click **Start**. The line shows *Waiting for the service to change state…*, then
   *Navigation filter is running*.
2. The state strip changes to **Initializing**, and the UDP output to **Streaming**.
3. Continue with the [Alignment SOP](#5-alignment-sop).

If there is no control line, control from the web page is turned off on this device
(`nav.allow_control`). Start AIO NAV the usual way (the AIO Nav desktop app or
`sudo systemctl start aio-nav`); the dashboard detects it either way.

### 4.3 During operation

- Keep an eye on the state strip. **Ready** means the output can be used.
- Amber advisories (such as *GNSS signal unavailable*) do not stop navigation, but plan
  for reduced accuracy. Check the ± values on the Navigation page.
- If the state becomes **Fault**, follow the reason shown in the strip
  ([Troubleshooting](#9-troubleshooting)).

### 4.4 Restarting or stopping

- **Restart** (asks for confirmation): stops and starts AIO NAV. **Alignment starts over**,
  so only restart while the vehicle can stand still.
- **Stop** (asks for confirmation): ends the navigation output. Your application stops
  receiving data.

### 4.5 After the run

1. Stop the vehicle and click **Stop** if no more navigation output is needed.
2. Open the **Data** page and download the log files of the run (section 7.2).

## 5. Alignment SOP

Alignment is how the filter finds its initial attitude (roll, pitch, heading) and position
before the output can be trusted. It has three stages, shown by three lamps.

### 5.1 Stages

| Stage | Lamp turns green | What the filter does | Operator action |
|-------|------------------|----------------------|-----------------|
| 1. Coarse alignment (leveling) | **Alignment** | Measures gravity to find roll and pitch. Takes the initial position from GNSS. | Keep the vehicle **completely still**, engine running is fine, nobody moving in or on it. Default duration: 10 s. |
| 2. Initial heading | **Initial heading** | Finds the heading. With the default setting, heading comes from GNSS while the vehicle moves. | When **Alignment** is green, drive off **straight** at a steady speed under open sky until the lamp turns green. |
| 3. Fine alignment | **Fine alignment** | Refines heading and sensor errors until heading accuracy is within 1°, or until the fine-alignment time limit passes (default 300 s). | Keep driving normally with GNSS available. Include a few turns and speed changes. |

When all lamps required on this device are green (by default all three), the state strip
turns **Ready**.

### 5.2 Step by step

1. Park the vehicle at the start point under open sky. GNSS lamp should be green once
   the filter runs.
2. On the Overview page, click **Start**. The state becomes **Initializing**: *Waiting for
   alignment*.
3. **Do not move** for at least 10 s, until the **Alignment** lamp turns pale green.
   While stationary, the Navigation page shows **ZUPT** active; this is expected.
4. Drive straight ahead at a steady speed. Watch **Initial heading** turn green.
5. Keep driving with some turns until **Fine alignment** turns green. Check on the
   Navigation page that heading ± is about 1° or better.
6. The strip shows **Ready**. Navigation output can be used.

### 5.3 If alignment goes wrong

| Situation | Action |
|-----------|--------|
| The vehicle moved during stage 1 | Stop the vehicle, click **Restart**, and repeat from step 3. |
| **Alignment** stays amber for well over 10 s | Check the GNSS lamp (initial position comes from GNSS) and that the vehicle is really still. Then restart. |
| **Initial heading** does not turn green while driving | GNSS may be blocked (trees, buildings, tunnel). Drive in an open area. |
| **Fine alignment** takes long | It completes at the latest when the fine-alignment time limit passes (default 300 s). Keep driving in open sky. |
| GNSS is lost after Ready | Navigation continues on inertial sensors (advisory only). Accuracy degrades over time; watch the ± values. |

> The alignment method and times are set in AIO NAV's own settings file (`aio_nav.yaml`,
> section `Alignment`): `coarse_duration` (default 10 s), `fine_duration` (300 s),
> `fine_heading_std_threshold` (1°) and the attitude option. With `att.option: 2`
> the heading comes from the settings file instead of from driving, and stage 2 needs no
> motion. Which lamps are required for Ready is set by `nav.ready_requires` in the
> dashboard settings. The minimum speed and distance needed for the heading depend on the
> navigation library. Confirm the values for your vehicle with the navigation team.

## 6. Navigation output over UDP (data integration)

### 6.1 Overview

| Item | Value |
|------|-------|
| Transport | UDP over IPv4, one packet per epoch, no acknowledgement |
| Rate | `output_rate` in `aio_nav.yaml` (default 100 Hz) |
| Packet | 156 bytes, binary, little-endian |
| Destinations | `127.0.0.1:9000` on the device (used by the dashboard) **and** one external `host:port` (`output_udp` in `aio_nav.yaml`) |
| Starts | As soon as AIO NAV runs, **before** alignment completes. Use the flags to know when the data is valid. |

Each destination gets its own copy of every packet. Your receiver and the dashboard do
not compete for data.

### 6.2 Setting the destination

1. On the device, edit AIO NAV's settings:
   ```bash
   nano ~/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml
   ```
   ```yaml
   output_udp: "192.0.2.50:9000"     # your computer's IP and port
   ```
2. Restart AIO NAV (**Restart** on the Overview page), then restart the dashboard
   (`aio-dashboard restart`) so it shows the new destination under **Sending to**.
3. On the receiving computer, allow incoming UDP on that port in the firewall.

Only **one** external destination is supported, and it must be a unicast IPv4 address.
To feed several applications, receive on one computer and forward from there.

### 6.3 Packet layout

| Bytes | Field | Type | Value |
|-------|-------|------|-------|
| 0–1 | Sync | 2 × uint8 | `0x55 0xAA` |
| 2 | Message ID | uint8 | `0x04` (navigation solution) |
| 3–4 | Payload length | uint16 | `150` |
| 5–154 | Payload | 150 bytes | See 6.4 |
| 155 | Checksum | uint8 | Sum of bytes 2–154, modulo 256 |

Discard any packet whose sync, ID, length or checksum does not match.

### 6.4 Payload fields

Offsets are from the start of the packet (payload starts at byte 5).
Python `struct` format of the payload: `<Qdd28fH3f`.

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
| 141 | flags | uint16 | — | Status bits, see 6.5 |
| 143 | odometer scale | float32 | — | Used when odometry aiding is configured |
| 147 | mount pitch | float32 | deg | Estimated sensor mounting angles |
| 151 | mount yaw | float32 | deg | |

### 6.5 Status flags

Only the low byte of `flags` is used, and it is sent **bit-reversed**. Reverse the 8 bits
first, then read:

| Bit (after reversing) | Bit on the wire | Name | Meaning when 1 |
|----:|----:|------|----------------|
| 0 | 7 | ZUPT | Zero-velocity update applied (vehicle stationary) |
| 1 | 6 | ZIHR | Zero heading-rate update applied |
| 2 | 5 | NHC | Non-holonomic constraint applied (no sideways/vertical slip) |
| 3 | 4 | GNSS | GNSS measurement applied in this epoch |
| 4 | 3 | VUPT | Odometry / vehicle velocity update applied |
| 5 | 2 | ALIGN | Coarse alignment complete |
| 6 | 1 | HEADING | Initial heading valid |
| 7 | 0 | FINE | Fine alignment complete |

**When is the data valid?** Use the solution only when ALIGN, HEADING and FINE are all 1
(the same rule as the dashboard's **Ready**). ZUPT, ZIHR, NHC, GNSS and VUPT change from
epoch to epoch and only report which aiding was used; GNSS updates arrive at the GNSS
rate, so the GNSS bit is set only in some packets.

### 6.6 Receiver example (Python 3, no extra packages)

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
        continue                              # not a valid NAV packet
    f = PAYLOAD.unpack_from(pkt, 5)
    flags = reverse_bits8(f[31])
    active = [n for i, n in enumerate(FLAG_NAMES) if flags >> i & 1]
    print(f"t={f[0] / 1e6:.3f} lat={f[1]:.8f} lon={f[2]:.8f} h={f[3]:.2f} "
          f"vn={f[4]:.2f} ve={f[5]:.2f} vu={f[6]:.2f} hdg={f[9]:.1f} {' '.join(active)}")
```

For C/C++, use a packed struct (`#pragma pack(push, 1)`) with the field order of 6.4:
`uint64_t time_us; double lat, lon; float alt, vn, ve, vu, roll, pitch, heading, 9 × σ,
12 × IMU errors; uint16_t flags; float odo_scale, mount_pitch, mount_yaw;` (150 bytes).

### 6.7 Checking the integration

1. On the dashboard, **UDP output** shows **Streaming** and **Sending to** shows your
   computer's address and port.
2. Your receiver prints packets at the expected rate (100 per second by default).
3. Latitude, longitude and heading match the Navigation page.
4. The flags show ALIGN, HEADING and FINE when the dashboard shows **Ready**.

## 7. Other outputs: ROS 2 topics and log files

### 7.1 ROS 2 topics (development interface)

AIO NAV also publishes on ROS 2. These topics are meant for internal development; the UDP
output is the product interface. They are published **only after coarse alignment**.

| Topic | Type | Content |
|-------|------|---------|
| `/nav/fix` | `sensor_msgs/NavSatFix` | WGS84 position; `status.service` carries the flags (not bit-reversed) |
| `/nav/odometry` | `nav_msgs/Odometry` | TWD97 position, ENU velocity, covariances |
| `/nav/path` | `nav_msgs/Path` | Recent track |
| TF `map` → `imu_link` | — | Pose in the TWD97 map frame |

ROS domain and network settings must match AIO NAV's (default domain 13).

### 7.2 Log files (Data page)

When enabled in `aio_nav.yaml` (`save_fusion_txt`, `save_parsed_txt`), AIO NAV writes
tab-separated text logs once alignment is complete, in its `output/` folder:

| File | Content |
|------|---------|
| `…_fusion.txt` | Navigation solution at the output rate: time, position, velocity, attitude, accuracy, IMU errors, flags |
| `…_imu.txt`, `…_gnss.txt`, `…_odom.txt` | The raw sensor inputs, parsed |

Download them from the **Data** page of the product view.

## 8. Maintenance view (engineers)

`http://<device-ip>:8081`. The sidebar lamps show at a glance which page has a problem.

| Page | Use it to |
|------|-----------|
| **Overview** | See the four layers (System, Services, Data flow, Network) and the list of active issues. Each issue links to the page that explains it. |
| **System** | CPU, GPU, memory, disk, temperatures, systemd services, network interfaces. |
| **ROS 2** | Required nodes, monitored topics (rate, freshness, publishers, subscribers), all topics. |
| **Camera / LiDAR** | Reachability, driver state, topic health, **Live view** (camera image; LiDAR point cloud with top/3D view), **Run diagnostic**, **Restart driver** (asks for confirmation). |
| **Network** | Interfaces and addresses, sensor reachability, the NAV output destinations and their route. |
| **Diagnostics** | Active faults and warnings, results of diagnostics and restarts, and the event history. |

The Live view reads the sensor's ROS topic only while its page is open.
Device commands:

```bash
aio-dashboard status     # service state and web addresses
aio-dashboard logs       # follow the log
aio-dashboard restart
aio-dashboard config     # edit settings, check, restart
```

## 9. Troubleshooting

| What you see | Likely cause | What to do |
|--------------|--------------|------------|
| Page does not open | Wrong address, network, or dashboard not running | Check the IP (`aio-dashboard urls` on the device) and the cable; run `aio-dashboard status`. |
| **403 Forbidden** | Your computer is not in the allowed list | Ask the administrator to add your IP (`access.allowed_clients`). |
| Grey strip, "Lost connection to the dashboard" | Your browser cannot reach the device | Check the network; the page recovers by itself. |
| **Fault**: AIO NAV process is not running | The filter is stopped | Click **Start** (section 4.2). |
| **Fault**: NAV output stopped | The filter froze or its sensors stopped | Check the IMU/GNSS driver on the Maintenance view; **Restart** AIO NAV. |
| AIO NAV line: "The service stopped with an error" | AIO NAV crashed | Click **Start** again. If it repeats, collect `journalctl -u aio-nav -n 100` for the navigation team. |
| AIO NAV line: "aio-nav.service is not installed" | Web control is not set up on this device | Start AIO NAV the usual way; ask the administrator to re-run the installer. |
| Stuck at **Initializing** | Alignment not finished | Follow the [Alignment SOP](#5-alignment-sop) and 5.3. |
| **GNSS signal unavailable** advisory | Sky view blocked, antenna or GNSS receiver issue | Navigation continues; move to open sky; check the antenna. |
| **Low rate** / **Stale** | Device overloaded or filter input interrupted | Check the System page (CPU, temperature) and the ROS 2 page. |
| Camera/LiDAR **Not connected** | Sensor powered off, cable, or wrong IP | Check power and cable; Maintenance → Camera/LiDAR → **Run diagnostic**. |
| Your application receives nothing, dashboard says Streaming | Destination or firewall | Check **Sending to** matches your computer; open the UDP port in its firewall (section 6.7). |
| No files on the Data page | Logging is off or no run has been aligned yet | Enable `save_fusion_txt` in `aio_nav.yaml`; logs start after alignment. |

## 10. Glossary

| Term | Meaning |
|------|---------|
| AIO NAV | The navigation filter that runs on the device (`aio_nav_node`). |
| Alignment | Finding the initial attitude and position before navigation. |
| GNSS | Satellite positioning (GPS, Galileo, BeiDou, …). |
| IMU | Inertial measurement unit: gyroscopes and accelerometers. |
| ZUPT | Zero-velocity update: the filter uses "the vehicle is not moving". |
| ZIHR | Zero integrated heading rate: the filter uses "the heading is not changing" while stationary. |
| NHC | Non-holonomic constraint: a car does not slide sideways or jump vertically. |
| VUPT | Vehicle velocity update from odometry. |
| σ (sigma), ± | Estimated 1-sigma accuracy of a value. |
| TWD97 | Taiwan's national map coordinate system, used by the ROS outputs. |
| GPS time of week | Seconds since the start of the GPS week (Sunday 00:00 GPS time). |
