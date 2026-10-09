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
| **Maintenance view** | `http://<device-ip>:8081` (password) | Engineers | Pinpoint whether a problem is in the system, a service, ROS, a sensor or the network; live camera and LiDAR view; driver diagnostics and restarts; starting and stopping the sensor drivers; editing AIO NAV's settings; a terminal; bag replay (optional module). |
| **`aio-dashboard` command** | Device terminal | Engineers | Start, stop, check and configure the dashboard itself. |

The dashboard starts and stops AIO NAV (when enabled) and watches everything else. AIO NAV
runs on its own: navigation keeps running even if the dashboard stops or restarts, or a browser
is closed.

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

### 3.1 The status area

A colored area across the top of every page answers one question: *can I use the
navigation output right now?* It shows the state, a line saying what is happening, a line
saying what to do next, and (on Overview and Navigation) the **Start**, **Restart** and
**Stop** buttons. Red is kept for real problems with AIO NAV.

![Overview page while Ready](images/user/overview-ready.png)
*The Overview page: status area with the AIO NAV buttons, output line, map with the recent track,
position and attitude, and the status lamps.*

| State | Color | Meaning | What to do |
|-------|-------|---------|------------|
| **Stopped** | Light grey | AIO NAV is not running and nobody asked it to run. This is normal, not a problem. | Click **Start** when you want navigation. |
| **Starting** | Teal | AIO NAV was started and is getting ready; no navigation output yet. | Wait. If nothing comes out within 60 s it becomes **Fault**. |
| **Initializing** | Amber | AIO NAV is running and sends output, but alignment is not finished. It shows the current step and what to do, for example *Step 1 of 3: Alignment — Keep the vehicle completely still*. | Follow the instruction ([Alignment SOP](#5-alignment-sop)). Do not use the output yet. |
| **Ready** | Green | AIO NAV is running, data is fresh, and alignment is complete. The output can be used. | Operate normally. |
| **Fault** | Red | AIO NAV has a problem: it stopped although nobody pressed Stop, it runs but has sent nothing for 60 s after starting, or its output stopped for more than 2 s. The reason is shown. | See [Troubleshooting](#9-troubleshooting). |
| **Unknown** / **Offline** | Dark grey | The dashboard just started, or your browser lost its connection to the device. | Wait a few seconds; check the network if it persists. |

![A fault: what happened and what to do](images/user/status-fault.png)
*A fault always says what happened and what to do.*

A state only changes after the condition has lasted about a second, and only returns to
Ready after about 2 stable seconds. A single lost packet never makes it flicker. While AIO
NAV runs, the area also shows how long it has been running, and **Bag replay** when it
processes a recorded bag instead of the live sensors.

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
| **Alignment** | Step 1, coarse alignment (leveling). |
| **Initial heading** | Step 2, the filter has a valid heading. |
| **Fine alignment** | Step 3, heading accuracy reached its target. |
| **GNSS** | Position quality of the GNSS receiver: **green** = *RTK fix* (centimeter level), **amber** = *RTK float* (about 10 cm, less accurate), **red** = *SPP* (single-point positioning, meters) or *No signal* (no usable position; the advisory under the lamps says why, e.g. the receiver sends no messages). *Fix (type unknown)* is a position whose type the receiver did not report (amber when better than 1 m). Amber and red are warnings only: inertial navigation continues, but accuracy slowly degrades. |
| **Camera / LiDAR** (if fitted) | The sensor answers on the network. "Not set up" means no IP is configured; this is normal if not needed. |
| **Network** | The sensor network link is up. |
| **ZUPT / ZIHR / NHC / VUPT** (Navigation page) | Which aiding the filter is using at the moment (see the [glossary](#10-glossary)). Cyan = active, grey = idle. Idle is normal. |

The three alignment lamps are steps in order: **Done** (pale green), **In progress** (amber,
the current step), **Waiting** (grey, a later step), or **—** while AIO NAV sends no output.

GNSS, camera, LiDAR and network problems are shown as **advisories** (amber bars under
the lamps). They never change the state from Ready to Fault, because the navigation
solution is still usable. A red GNSS lamp adds the advisory *GNSS: SPP* or *GNSS: No signal*.

### 3.3 Output line

One line under the status area tells whether the navigation output (UDP, section 6) is being
sent, at what rate and to where, for example *Streaming · 100 Hz to 192.0.2.50:9000*.

| Label | Meaning |
|-------|---------|
| **Off** | AIO NAV is stopped, so nothing is sent. Normal. |
| **Waiting** | AIO NAV is starting; nothing sent yet. |
| **Streaming** | The device is sending navigation packets at the expected rate. |
| **Low rate** | Packets are sent, but at under 80% of the expected rate. |
| **Stale** | No packet for more than 0.5 s. |
| **Lost** | No packet for more than 2 s while AIO NAV should be sending. The state becomes Fault. |

UDP has no acknowledgement: "Streaming" confirms that the device is sending, not that your
application received it.

### 3.4 Pages

- **Overview**: status area with the AIO NAV buttons, output line, map with the last 60 s of track,
  position, heading (with an arrow; up = north), speed, roll and pitch, lamps,
  advisories, storage.
- **Navigation**: large map with the whole track of this run, and every value with its
  accuracy (±): position, velocity north/east/up, speed, attitude, NAV time. Aiding lamps
  and the output line (with the packet count) are at the bottom.
  **Show whole trajectory** zooms to the full track; **Follow vehicle** re-centers.
- **Data**: AIO NAV log folders. Click a folder to open it and **Download** to save a
  file. Nothing on the device can be changed from this page.

![Navigation page](images/user/navigation.png)
*Navigation page: the whole track of this run, every value with its accuracy, the aiding lamps.*

![Data page](images/user/data.png)
*Data page: download the AIO NAV logs.*

<img src="images/user/overview-phone.png" alt="Overview on a phone" width="300">

*The pages also work on a phone or tablet.*

## 4. Daily operation

### 4.1 Before starting

- [ ] The device is powered and the GNSS antenna has a clear view of the sky.
- [ ] The vehicle is parked in its starting position and will stay **completely still**
      for the first part of alignment.
- [ ] Live use: the mode is **Live** and the sensor drivers are running (section 4.6; Maintenance → ROS 2).
- [ ] Your computer opens the product view (section 2).
- [ ] The status area shows **Stopped** (or a running state), not Unknown/Offline: the dashboard is connected.

### 4.2 Starting AIO NAV

The **Start** button is in the status area at the top of the Overview and Navigation pages.

![Stopped, before Start](images/user/overview-stopped.png)
*Stopped: nothing is wrong, AIO NAV simply is not running. Click Start.*

1. Click **Start**. The state becomes **Starting** (teal), then **Initializing** (amber) as
   soon as AIO NAV sends output; the output line changes from **Off** to **Streaming**.
2. Follow the step shown in the status area ([Alignment SOP](#5-alignment-sop)).

![Initializing, step 1 of 3](images/user/overview-initializing.png)
*Initializing: the status area names the current alignment step and what to do; the step's lamp is
amber, later steps are grey.*

If the state becomes **Fault** with *AIO NAV stopped unexpectedly*, or the message under the
buttons reads *AIO NAV did not start: …*, see [Troubleshooting](#9-troubleshooting). If there
are no buttons, control from the web page is turned off on this device (`nav.allow_control`);
ask the administrator to enable it. AIO NAV that was started in another way is detected too.

### 4.3 During operation

- Keep an eye on the status area. **Ready** means the output can be used.
- Amber advisories (such as *GNSS: SPP* or *GNSS: No signal*) do not stop navigation, but plan
  for reduced accuracy. Check the ± values on the Navigation page.
- If the state becomes **Fault**, follow the reason shown in the status area
  ([Troubleshooting](#9-troubleshooting)).

### 4.4 Restarting or stopping

- **Restart** (asks for confirmation: choose **Restart now**, or **Cancel** / Esc): stops and starts AIO NAV. **Alignment starts over**,
  so only restart while the vehicle can stand still.
- **Stop** (asks for confirmation: choose **Stop now**, or **Cancel** / Esc): ends the navigation output. Your application stops
  receiving data.

### 4.5 After the run

1. Stop the vehicle and click **Stop** if no more navigation output is needed.
2. Open the **Data** page and download the log files of the run (section 7.2).

### 4.6 Replaying a recorded bag (engineers)

Bag replay is part of the optional **replay module** (installed with
`sudo deploy/install.sh --user <you> --with-replay`). Without it the machine always runs **Live**
and the steps below are not available.

Everything is on Maintenance → **Replay**:

![Replay page](images/maintenance/replay.png)
*Replay page: ROS environment, the checks before playing, the bags found, and the playback settings
with the exact command.*

1. **Before playing** lists what has to be true, each with a button that fixes it:
   - the ROS environment is **Bag replay** (button *Switch to Bag replay*: AIO NAV and DSO are
     stopped and the dashboard restarts, a few seconds). Bag replay uses `aio_nav_bag.yaml`
     (ROS domain 13, simulated time) instead of `aio_nav.yaml` (domain 10, this computer only);
   - the sensor drivers do not publish into the bag's domain. They normally run in the Live domain
     (10), which the bag domain (13) does not see, so they may keep running (they still use CPU
     and camera bandwidth). Only if they run in the bag's domain, or their domain cannot be read,
     do they have to be stopped (button *Stop Sensor drivers*);
   - no other bag is playing.
2. On the Overview page click **Start** so AIO NAV processes the bag (the player works without it,
   but nothing is computed).
3. Under **Bags**, click a bag. The list shows every bag found in the configured folders
   (default: the home folder, two levels deep), with recording time, length, size and topics.
4. Tick the **topics** to play (none ticked: all), then set **Rate**, **Start at**, **Loop** and
   **Start paused**. **Advanced** has every other `ros2 bag play` option (`/clock` rate, delay,
   read-ahead queue, topic remapping, storage plugin, QoS overrides file, storage config file, wait
   for all acked, loaned messages, log level). The exact command is shown underneath; it can also
   be pasted into a terminal.
5. Click **Play**. **Playback** shows the estimated position, and has **Pause** / **Resume**, a
   **Rate** change while playing, and **Stop**. The player's output is under *Player output*.
6. **Save preset** keeps a bag with its topics and options under a name; pick it from **Presets…**
   next time. The last settings used with each bag are also remembered.
7. Back to live use: **Stop** the playback, switch the ROS environment to **Live** on the same
   page, start the sensor drivers if they were stopped (Maintenance → ROS 2; they publish after
   about 15 s), then click **Start** on the Overview page.

Playing from a terminal instead: use the bag domain and the clock, because a terminal may default
to another domain (`echo $ROS_DOMAIN_ID`):
```bash
ROS_DOMAIN_ID=13 ROS_LOCALHOST_ONLY=0 ros2 bag play <bag folder> --clock 100
```

## 5. Alignment SOP

Alignment is how the filter finds its initial attitude (roll, pitch, heading) and position
before the output can be trusted. It has three stages, shown by three lamps.

### 5.1 Stages

| Stage | Lamp turns green | What the filter does | Operator action |
|-------|------------------|----------------------|-----------------|
| 1. Coarse alignment (leveling) | **Alignment** | Measures gravity to find roll and pitch. Takes the initial position from GNSS. | Keep the vehicle **completely still**, engine running is fine, nobody moving in or on it. Default duration: 10 s. |
| 2. Initial heading | **Initial heading** | Finds the heading. With the default setting, heading comes from GNSS while the vehicle moves. | When **Alignment** is green, drive off **straight** at a steady speed under open sky until the lamp turns green. |
| 3. Fine alignment | **Fine alignment** | Refines heading and sensor errors until heading accuracy is within 1°, or until the fine-alignment time limit passes (default 300 s). | Keep driving normally with GNSS available. Include a few turns and speed changes. |

When all lamps required on this device are green (by default all three), the status area
turns **Ready**.

### 5.2 Step by step

1. Park the vehicle at the start point under open sky. Wait until the GNSS lamp shows
   **RTK fix** (green): the initial position comes from GNSS. **RTK float** (amber) works but
   starts less accurately.
2. On the Overview page, click **Start**. After **Starting**, the state becomes
   **Initializing**: *Step 1 of 3: Alignment — Keep the vehicle completely still*.
3. **Do not move** for at least 10 s, until the **Alignment** lamp turns pale green.
   While stationary, the Navigation page shows **ZUPT** active; this is expected.
4. Drive straight ahead at a steady speed. Watch **Initial heading** turn green.
5. Keep driving with some turns until **Fine alignment** turns green. Check on the
   Navigation page that heading ± is about 1° or better.
6. The status area shows **Ready**. Navigation output can be used.

### 5.3 If alignment goes wrong

| Situation | Action |
|-----------|--------|
| The vehicle moved during stage 1 | Stop the vehicle, click **Restart**, and repeat from step 3. |
| **Alignment** stays amber for well over 10 s | Check the GNSS lamp (initial position comes from GNSS; it should not be red) and that the vehicle is really still. Then restart. |
| **Initial heading** does not turn green while driving | GNSS may be blocked (trees, buildings, tunnel). Drive in an open area. |
| **Fine alignment** takes long | It completes at the latest when the fine-alignment time limit passes (default 300 s). Keep driving in open sky. |
| GNSS is lost after Ready | Navigation continues on inertial sensors (advisory only). Accuracy degrades over time; watch the ± values. |

> The alignment method and times are set in AIO NAV's own settings file (`aio_nav.yaml`,
> section `Alignment`): `coarse_duration` (default 10 s), `fine_duration` (300 s),
> `fine_heading_std_threshold` (1°) and the attitude option (all on Maintenance → **AIO NAV
> config**). With `att.option: 2`
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
not compete for data. The dashboard is the only program that listens on `127.0.0.1:9000`:
only one program can hold that address, so do not run another listener there (for example the
old AIO Nav desktop app), or the dashboard would show no data.

### 6.2 Setting the destination

1. Set **UDP destination** to your computer's IP and port (for example `192.0.2.50:9000`) on
   Maintenance → **AIO NAV config** (section 8), in the file of the mode you use: `aio_nav.yaml`
   (Live) and `aio_nav_bag.yaml` (Bag replay); change both if you use both. Or edit the file on
   the device:
   ```bash
   nano ~/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml
   ```
   ```yaml
   output_udp: "192.0.2.50:9000"     # your computer's IP and port
   ```
2. Restart AIO NAV (**Save and restart AIO NAV** on the config page, or **Restart** on the
   Overview page). The dashboard's **Output** line follows the file by itself within a few seconds.
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

1. On the dashboard, the **Output** line shows **Streaming** and your computer's address and
   port.
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

ROS domain and network settings must match AIO NAV's: **Live** uses domain 10 (this computer only), **Bag replay** uses domain 13 (network). Maintenance → ROS 2 shows the one in use; the Overview shows a **Bag replay** label when it is not Live.

### 7.2 Log files (Data page)

When enabled in `aio_nav.yaml` (`save_fusion_txt`, `save_parsed_txt`), AIO NAV writes
tab-separated text logs once alignment is complete, in the `output/` folder of aio-nav-ros (next to `install/`; both settings files point there):

| File | Content |
|------|---------|
| `AIO-NAV.txt` (the name set by `fusion_txt_path`) | Navigation solution at the output rate: time, position, velocity, attitude, accuracy, IMU errors, flags |
| `AIO-NAV_imu.txt`, `AIO-NAV_gnss.txt`, `AIO-NAV_odom.txt` | The raw sensor inputs, parsed (`save_parsed_txt`) |

Download them from the **Data** page of the product view.

## 8. Maintenance view (engineers)

`http://<device-ip>:8081`. The sidebar lamps show at a glance which page has a problem.

| Page | Use it to |
|------|-----------|
| **Overview** | See the four layers (System, Services, Data flow, Network) and the list of active issues. Each issue links to the page that explains it. |
| **System** | CPU, GPU, memory, disk, temperatures, systemd services, network interfaces. |
| **ROS 2** | Required nodes, monitored topics (rate, freshness, publishers, subscribers), all topics. Also here: **Sensor drivers** (Start / Stop / Restart the camera and IMU/GNSS drivers) and the ROS environment in use. |
| **Replay** (replay module only) | Switch between Live and Bag replay, and play bags with any `ros2 bag play` option (section 4.6). |
| **Camera / LiDAR** (if fitted) | Reachability, driver state, topic health, **Live view** (camera image; LiDAR point cloud with top/3D view), **Run diagnostic**, **Restart driver** (asks for confirmation). |
| **Network** | Interfaces and addresses, sensor reachability, the NAV output destinations and their route. |
| **Diagnostics** | Active faults and warnings, results of diagnostics and restarts, and the event history. |
| **AIO NAV config** | Edit AIO NAV's settings files (`aio_nav.yaml` for Live, `aio_nav_bag.yaml` for Bag replay). **Settings** is a form of the most used values (output, ROS, logs, alignment, aiding, lever arms); **Edit file** is the whole file. Comments in the file are kept. **Review changes** checks the file and shows exactly what changes; then **Save**, or **Save and restart AIO NAV** so it takes effect at once. Every save keeps the previous version (**Earlier versions**, to go back). |
| **Terminal** | A shell on the device, in the browser, for quick checks (`ros2 topic hz …`, `journalctl …`, `top`). It runs as the dashboard's user and loads your `.bashrc`, like a desktop terminal (so ROS is set up); **Use active ROS domain** types the export line for the mode in use. Up to 3 at once; closed after 30 min without typing. |

The maintenance view can change the device, so it needs a password; the product view does not.
Set it on the device with `aio-dashboard password` (there is no default password: until one is set,
the maintenance view stays locked and its sign-in page says how to set one). Opening any
maintenance page shows the sign-in page first, then the page you asked for. You are signed out after
15 minutes without use (opening pages and taking actions count as use; the pages refreshing
themselves do not), or with **Sign out** at the bottom of the sidebar. Sign-ins, failed attempts,
saved settings and opened terminals are recorded in the event log (Diagnostics).

The Live view reads the sensor's ROS topic only while its page is open. A deliberately stopped
driver is not reported as a problem.

![Sign-in page](images/maintenance/login.png)
*Sign-in page of the maintenance view.*

![Maintenance overview](images/maintenance/overview.png)
*Overview: the four layers and the active issues. The sidebar lamps point to the page with a problem;
the tool pages (AIO NAV config, Terminal, Replay) have no lamp.*

![System page](images/maintenance/system.png)
*System: resources, storage, temperatures and services.*

![ROS 2 page](images/maintenance/ros.png)
*ROS 2: the environment in use, the sensor drivers (Start / Stop / Restart), nodes and topics.*

![Camera page with Live view](images/maintenance/camera.png)
*Camera: reachability, driver, topic and the Live view (a simulated picture here).*

![Network page](images/maintenance/network.png)
*Network: interfaces, sensor reachability and where the navigation output goes.*

![Diagnostics page](images/maintenance/diagnostics.png)
*Diagnostics: active issues, action results and the event history.*

![AIO NAV config, Settings](images/maintenance/config-form.png)
*AIO NAV config: the most used settings of the file in use; **Edit file** shows the whole file.*

![AIO NAV config, Review changes](images/maintenance/config-review.png)
*Review changes: the exact lines that change, checked before saving.*

![Terminal](images/maintenance/terminal.png)
*Terminal: a shell on the device, for example to check a topic rate.*

Device commands:

```bash
aio-dashboard status     # service state and web addresses
aio-dashboard logs       # follow the log
aio-dashboard restart
aio-dashboard config     # edit settings, check, restart
aio-dashboard password   # set the maintenance password
```

## 9. Troubleshooting

| What you see | Likely cause | What to do |
|--------------|--------------|------------|
| Page does not open | Wrong address, network, or dashboard not running | Check the IP (`aio-dashboard urls` on the device) and the cable; run `aio-dashboard status`. |
| **403 Forbidden** | Your computer is not in the allowed list | Ask the administrator to add your IP (`access.allowed_clients`). |
| Maintenance view: sign-in page says no password is set | No maintenance password yet | On the device: `aio-dashboard password`, then reload the page. |
| Maintenance view: back on the sign-in page | Signed out after 15 minutes without use | Sign in again; the page you were on opens afterwards. |
| A page looks unstyled or shows *Unknown* right after an update | The browser still has the old page files | Reload with **Ctrl+Shift+R**. |
| **Offline**, "Lost connection to the dashboard" | Your browser cannot reach the device | Check the network; the page recovers by itself. |
| **Stopped** | AIO NAV is not running (normal) | Click **Start** (section 4.2). |
| **Fault**: AIO NAV stopped unexpectedly | AIO NAV exited by itself | Click **Start**. If it keeps stopping, collect `/opt/aio-dashboard/logs/aio_nav.log` for the navigation team (Maintenance view shows details). |
| **Fault**: AIO NAV is running but sends no navigation output | Nothing came out within 60 s of starting (`nav.startup_timeout_s`) | **Restart** AIO NAV; check its sensors (IMU/GNSS driver) on the Maintenance view. |
| **Fault**: Navigation output stopped | The filter froze or its sensors stopped | Check the IMU/GNSS driver on the Maintenance view; **Restart** AIO NAV. |
| Message *AIO NAV did not start: …* under the buttons | AIO NAV exited or could not start; the message gives the reason | Click **Start** again. If it repeats, collect `/opt/aio-dashboard/logs/aio_nav.log` for the navigation team. |
| **Fault**: AIO NAV is not installed on this device | The aio-nav-ros `install/` folder is missing or incomplete on this device | Ask the administrator to check that aio-nav-ros is installed. |
| Live: no camera or IMU data | The sensor drivers are stopped | Maintenance → ROS 2 → Sensor drivers → **Start**; wait about 15 s. |
| Bag replay: AIO NAV receives no data | The bag is played in another ROS domain, or AIO NAV was not started | Section 4.6: play from Maintenance → Replay (it uses the right domain), and click Start on the Overview page. From a terminal: `ROS_DOMAIN_ID=13 ROS_LOCALHOST_ONLY=0 ros2 bag play … --clock 100`. |
| Stuck at **Initializing** | Alignment not finished | Follow the [Alignment SOP](#5-alignment-sop) and 5.3. |
| Event *DSO used too much memory: restarting DSO* (Maintenance → Diagnostics) | DSO kept growing (it was seen keeping every camera frame); the watchdog restarted it before the machine ran out of memory | Nothing to do right away; AIO NAV keeps running. If it repeats, report it to the DSO maintainers. The limit is `dso_watchdog.max_memory_mb`. |
| **GNSS** lamp amber or red (*RTK float*, *SPP*, *No signal*) | Sky view blocked, no RTK correction data, antenna or GNSS receiver issue | Navigation continues; move to open sky; check the antenna and the correction link. |
| **GNSS: No signal** with *no GNSS message for …* | The receiver driver publishes no position at all (no sky view, or the receiver/driver needs a restart) | Check the antenna and sky view; restart the sensor drivers (Maintenance → ROS 2); the driver log is `~/openrtk330-basler-driver.log`. |
| **Low rate** / **Stale** | Device overloaded or filter input interrupted | Check the System page (CPU, temperature) and the ROS 2 page. |
| Camera/LiDAR **Not connected** | Sensor powered off, cable, or wrong IP | Check power and cable; Maintenance → Camera/LiDAR → **Run diagnostic**. |
| Your application receives nothing, dashboard says Streaming | Destination or firewall | Check that the address on the **Output** line matches your computer; open the UDP port in its firewall (section 6.7). |
| No files on the Data page | Logging is off or no run has been aligned yet | Enable `save_fusion_txt` in `aio_nav.yaml`; logs start after alignment. |

## 10. Glossary

| Term | Meaning |
|------|---------|
| AIO NAV | The navigation filter that runs on the device (`aio_nav_node`). |
| Alignment | Finding the initial attitude and position before navigation. |
| GNSS | Satellite positioning (GPS, Galileo, BeiDou, …). |
| RTK fix / RTK float | Receiver solutions that use correction data: *fix* is centimeter level, *float* is about a decimeter. |
| SPP | Single-point positioning without corrections: meter-level accuracy. |
| Live / Bag replay | The two modes of the system: live sensors (ROS domain 10) or a recorded bag (domain 13). |
| IMU | Inertial measurement unit: gyroscopes and accelerometers. |
| ZUPT | Zero-velocity update: the filter uses "the vehicle is not moving". |
| ZIHR | Zero integrated heading rate: the filter uses "the heading is not changing" while stationary. |
| NHC | Non-holonomic constraint: a car does not slide sideways or jump vertically. |
| VUPT | Vehicle velocity update from odometry. |
| σ (sigma), ± | Estimated 1-sigma accuracy of a value. |
| TWD97 | Taiwan's national map coordinate system, used by the ROS outputs. |
| GPS time of week | Seconds since the start of the GPS week (Sunday 00:00 GPS time). |
