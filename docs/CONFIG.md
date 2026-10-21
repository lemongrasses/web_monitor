# AIO System Dashboard — Configuration Manual

[繁體中文版](CONFIG.zh-TW.md)

This manual explains where the dashboard's settings live, how to change them safely, and
what every setting means.

**Short version:** the shipped configuration works without edits. Most values are `auto`
(detected at start-up) or optional. You usually only add the camera and LiDAR IP
addresses, or pin a value that auto-detection got wrong.

---

## 1. Where the configuration file is

The installer copies the app to `/opt/aio-dashboard`. The running dashboard reads
**only** this file:

```
/opt/aio-dashboard/config/dashboard.yaml
```

The copy in the downloaded code (`~/web_monitor/config/dashboard.yaml`) is only a
template. It is copied into `/opt/aio-dashboard` once, on the first install. Editing it
afterwards changes nothing unless you re-install with `--reset-config` (see below).

| File | Read by the running dashboard? |
|------|--------------------------------|
| `/opt/aio-dashboard/config/dashboard.yaml` | **Yes** |
| `~/web_monitor/config/dashboard.yaml` | No. Template for new installs only |
| `/opt/aio-dashboard/config/dashboard.yaml.new` | No. Newest template, written when an update keeps your config |
| `/opt/aio-dashboard/config/dashboard.yaml.bak` | No. Your previous config, saved by `--reset-config` |

## 2. How to change a setting

1. Edit the installed file:
   ```bash
   sudo nano /opt/aio-dashboard/config/dashboard.yaml
   ```
   (Or run `aio-dashboard config`, which opens the editor, checks the file, and offers
   to restart in one step.)
2. Restart the dashboard. Settings are only read at start-up:
   ```bash
   sudo systemctl restart aio-dashboard
   ```
3. Check that it started and see what it detected:
   ```bash
   systemctl status aio-dashboard --no-pager
   journalctl -u aio-dashboard -n 40 --no-pager
   ```
   You can also open the Maintenance view (`http://<jetson-ip>:8081`). The **Network**
   page shows the detected `aio_nav.yaml` path and network interface. The **ROS 2** page
   shows which topics were picked.

**Extra step for restart permissions:** if you change which driver units can be
restarted (`restartable` or `unit` under `services`), re-run the installer as well. The
sudo permission that allows those restarts is generated from the installed config:
```bash
cd ~/web_monitor && sudo deploy/install.sh --user jetson
```

### Two ways to set up a new device

- **Install first, then adjust (recommended).** Run the installer, then edit
  `/opt/aio-dashboard/config/dashboard.yaml` only if something needs changing.
- **Edit first, then install.** Edit `~/web_monitor/config/dashboard.yaml` before the
  first install, and it gets copied in. On a device that already has the dashboard, run
  `sudo deploy/install.sh --user jetson --reset-config` to copy your edited template
  over the installed config. The old one is kept as `dashboard.yaml.bak`.

Updating the code (`git pull`, then running the installer without `--reset-config`)
never overwrites your installed config.

## 3. YAML rules that matter

- Indent with **spaces**, never tabs. Children are indented under their parent.
- Numbers and options you leave out use the built-in default. But `services`,
  `devices`, `ros.topics` and `ros.nodes` have **no** built-in entries: deleting one of
  these sections removes those items. Edit the shipped file rather than writing a new one.
- **Lists replace the default as a whole.** If you write `ros.topics`, write every
  topic you want, not just the new one. The same goes for `ready_requires`, `nodes` and
  `disk_paths`.
- Put text containing `:` or `#` in quotes: `host: "192.0.2.20"`.
- `auto` is a keyword: it turns on auto-detection for that setting.

A mistake never stops the dashboard:

- **The file cannot be read** (a YAML syntax error, or not a settings file): the dashboard runs
  with the settings that last started it (`state/dashboard.lastgood.yaml`), or with the built-in
  defaults if there are none, and raises the fault *Settings file has an error*.
- **A value of the wrong kind** (text where a number belongs, `yes` where `true` belongs): that
  value is replaced by its default, the log says `settings: <key>: … is not a valid value; using
  the default …`, and the fault *Settings with an invalid value: defaults used* is raised.

Check the file before restarting with `aio-dashboard check`, or:
```bash
python3 -c "import yaml; yaml.safe_load(open('/opt/aio-dashboard/config/dashboard.yaml')); print('OK')"
```

Relative paths (such as `logs/events.jsonl`) resolve against `/opt/aio-dashboard`.

---

## 4. Setting reference

### 4.1 `product` and `maintenance`: the two web pages

```yaml
product:      { host: 0.0.0.0, port: 8080 }
maintenance:  { host: 0.0.0.0, port: 8081 }
```

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `0.0.0.0` | Network address to listen on. `0.0.0.0` = all interfaces, so other computers can open the page. `127.0.0.1` = only this Jetson. |
| `port` | `8080` / `8081` | Port of the product view / maintenance view. |
| `maintenance.password_file` | `config/maint_password` | Where the hash of the maintenance password is kept (set it with `aio-dashboard password`). No file: what it protects stays locked. |
| `maintenance.lock` | `all` | `all`: the whole maintenance view needs the password. `tools`: only the AIO NAV config editor and the terminal do (the other maintenance pages are open, e.g. on a development machine). |
| `maintenance.unlock_minutes` | `15` | A signed-in browser is signed out after this many minutes without use (the pages' own refresh does not count). |
| `maintenance.config_test_start` | `true` | Before an AIO NAV settings file is saved on **Maintenance → AIO NAV config**, AIO NAV is started on it for a moment, cut off from the network; a file AIO NAV would not start with is refused, with what AIO NAV said. `false` saves after the checks alone. |
| `product.edit_lever_arms` | `true` | The user **Setup** page may change the lever arms in the Live `aio_nav.yaml` (no password, like Start / Stop; the values are checked, the previous file is kept and the change is logged). `false` shows them read-only. |

The product view is for operators. The maintenance view is for engineers and can
restart drivers. To keep the maintenance view reachable only from the Jetson itself,
set `maintenance.host: 127.0.0.1`.

### 4.1a `access`: who may open the pages

```yaml
access:
  allowed_clients: [192.0.2.50]
```

| Key | Default | Meaning |
|-----|---------|---------|
| `allowed_clients` | `[]` | IP addresses or CIDRs (e.g. `192.0.2.0/24`) of the computers allowed to open the product and maintenance views. Anyone else gets `403 Forbidden`. `127.0.0.1` (this Jetson) is always allowed. Empty = no restriction. |

Put the external computer on the sensor LAN here. Phones on the Wi-Fi hotspot are added
automatically (`wifi.hotspot.allow_clients`, section 4.5a). It is an access filter, not
authentication: it does not protect against a computer that spoofs that address on the same LAN.

### 4.2 `nav`: AIO NAV data and the Stopped / Starting / Initializing / Ready / Fault state

| Key | Default | Meaning |
|-----|---------|---------|
| `udp_bind` | `127.0.0.1:9000` | Where the dashboard listens for AIO NAV packets. `aio_nav_node` always sends to `127.0.0.1:9000`, so leave this unchanged. The dashboard is the only listener: the AIO Nav desktop app (`aio-nav-ui`) binds the same port, so do not run it together with the dashboard. |
| `aio_nav_config` | `auto` | Path to AIO NAV's `aio_nav.yaml`. The dashboard reads the UDP destination (`output_udp`) and rate (`output_rate`) from it to display them. `auto` searches `/home/*/aio-nav-ros/install/...` and `~/.local/opt/aio-nav-ros/...` and uses the newest file. The `AIO_NAV_CONFIG` environment variable also works. Edits to the file are picked up within about 2 s, and **Start** / **Restart** always re-read it first, so AIO NAV starts with what is on disk. If the edit changes the ROS domain or localhost setting of the mode in use, the dashboard restarts itself after the start (a few seconds; AIO NAV keeps running) so its own ROS connection follows. |
| `expected_rate_hz` | `null` | Expected packet rate. `null` = use `output_rate` from `aio_nav.yaml`. |
| `service` | `aio_nav` | Which entry in `services` is AIO NAV. |
| `allow_control` | `false` (shipped config: `true`) | `true` shows **Start / Restart / Stop** for AIO NAV on the Overview page. **Start** launches the filter and then DSO, **Stop** stops both, exactly like the AIO Nav desktop app (it does nothing for a process that is already running, so it never doubles one started from the app). The programs are the `aio-nav` and `aio-nav-dso` wrappers in the aio-nav-ros `install/` folder; no source code is needed. Stop and Restart ask for a second click. Only computers in `access.allowed_clients` can reach the page. |
| `control_group` | `[aio_nav, dso]` | Which `services` entries Start/Stop acts on, in start order. Each needs a `launch` setting. |
| `startup_grace_s` | `3.0` | For this long after the dashboard starts, it shows "Unknown" while it is still checking whether AIO NAV runs. |
| `startup_timeout_s` | `60` | After AIO NAV starts, it shows **Starting** until the first output arrives. With no output for this long the state becomes **Fault** ("running but sends no navigation output"). AIO NAV not running and not asked to run is **Stopped**, never a fault. |
| `stale_warn_s` | `0.5` | Packet age (seconds) at which UDP output shows **Stale**. |
| `stale_fault_s` | `2.0` | Packet age at which UDP output shows **Lost** and the state becomes **Fault**. |
| `low_rate_ratio` | `0.8` | UDP output shows **Low rate** when the measured rate is below this fraction of the expected rate. |
| `gnss_timeout_s` | `3.0` | GNSS shows **Unavailable** if no GNSS update arrived for this long. This is only an advisory. It never changes Ready. |
| `flag_active_s` | `1.0` | ZUPT, ZIHR, NHC and VUPT lamps stay **Active** for this long after the filter last used them. |
| `ready_requires` | `[alignment, heading_valid, fine_alignment]` | Filter flags that must all be set for **Ready**. Until then the state is **Initializing**. Options: `alignment`, `heading_valid`, `fine_alignment`. |
| `trajectory.recent_window_s` | `60` | How many seconds of recent track are drawn at full detail. |
| `trajectory.recent_hz` | `10` | Points per second kept for that recent track. |
| `trajectory.older_hz` | `1` | Points per second kept for older parts of the track. |

The overall state works like this:

- **Fault**: AIO NAV is not running, or no packet has arrived for `stale_fault_s`.
- **Initializing**: packets are arriving, but the `ready_requires` flags are not all set yet.
- **Ready**: AIO NAV is running, packets are fresh, and alignment is complete.

GNSS, camera, LiDAR and the network only produce advisories. They never turn Ready
into Fault. To show a change, a condition must last about 0.5–1 s; recovering back to
Ready must stay stable for about 2 s. These timings are built in and not configurable.

> The "Sending to …" address on the Overview page is **not** set here. It comes from
> `output_udp` in AIO NAV's `aio_nav.yaml`. Change it there, then restart AIO NAV and
> the dashboard.

### 4.2a `readiness`: what READY requires (and what the vehicle is told)

The dashboard keeps one *readiness* snapshot: the state for the vehicle (`BOOTING`,
`CHECKING`, `NAV_STARTING`, `KEEP_STILL`, `MOVE_STRAIGHT`, `REFINING`, `READY`, `DEGRADED`,
`FAULT`, `MAINTENANCE`), the action the operator should take, and bit masks of what is healthy
and of every reason the output must not be used. The pages, `GET /api/readiness` and the 0x05
status packet (section 4.2c) all show this same snapshot.

READY needs AIO NAV running with fresh output and the `nav.ready_requires` flags set, and, by
default, **RTK**: a GNSS position of at least RTK float (`require_rtk`). `require` adds further
conditions.

| Key | Default | Meaning |
|-----|---------|---------|
| `require_rtk` | `true` | READY needs a GNSS position of at least `gnss_min_quality` (RTK float). Switch it on the user **Settings** page (turning it off asks for a confirmation) or on **Maintenance → Overview → Readiness for the vehicle** (password); recorded in the event log and kept in `state/readiness.json`, which then wins over this value. Or set it here. |
| `require` | `[]` | Extra conditions for READY. Any of: `imu` (the IMU topic is healthy), `gnss` (the receiver reports a position), `gnss_quality` (at least `gnss_min_quality`), `dso` (DSO odometry is fresh and valid), `vehicle_route` (the kernel has a route to the `output_udp` destination), `storage` (disk below `system.disk_warn_percent`). |
| `stable_s` | `2.0` | A condition must hold this long before READY counts it. |
| `loss_s` | `1.0` | Once counted, a condition is dropped only after failing this long, so a single missed sample does not toggle READY. |
| `gnss_min_quality` | `float` | For `gnss_quality`: `spp`, `float` or `fixed`. A position the driver reports without a type but with good accuracy counts as float (it happens inside float periods). |
| `gnss_stable_s` | `3.0` | `stable_s` for `gnss_quality`. |
| `gnss_loss_s` | `3.0` | `loss_s` for GNSS: how long RTK may drop (trees, bridges) before READY becomes DEGRADED. Adjust per site. |
| `imu_topic` | `auto` | Which `ros.topics` entry is the IMU for `imu`. `auto` = the first one in group `nav`. Its *state* is used, not its raw age, so it also works with `ros.sampling`. |
| `dso_stale_s` | `1.0` | DSO odometry older than this is not ready. |
| `dso_only_with_vupt` | `true` | With `dso` in `require`, DSO is only required when `Odometry.vupt` is on in `aio_nav.yaml` (AIO NAV uses it). |
| `still_gyro_rad_s` | `0.05` | "Vehicle still", from the last 2 s of IMU samples: the largest rotation rate (rad/s). Parked with the engine on it stayed under 0.035; driving it was above 0.17. |
| `still_accel_std` | `0.15` | The spread of the acceleration magnitude (m/s²). Parked: under 0.16; driving: above 0.6. |
| `still_speed_m_s` | `0.3` | While the receiver has a GNSS position, its speed must also be below this (m/s). |
| `still_speed_topic` | `/openrtk330/odometry` | Where that speed comes from (the receiver's INS odometry). `""` = IMU only. |

"Vehicle still" is used before every automatic start (4.2b) and for **Save and restart** on the
Setup page. The four values above are defaults: limits measured on the vehicle on **Maintenance →
Still detection** replace them (kept in `state/still_limits.json`; *Back to defaults* removes them).

After READY was reached in a navigation run, losing a required condition (or the alignment)
gives **DEGRADED** with the action *stop using navigation*, never a return to the alignment
prompts: the vehicle may be moving. If AIO NAV itself stops after READY, the state is **FAULT**
with **SAFE_STOP** (*AIO NAV stopped after it was ready*), also across a dashboard restart, until
it is running again. A new run (AIO NAV restarted) starts the flow again.

> With RTK required, a site without RTK (only SPP) never becomes READY: switch RTK off there.
> An unknown name in `require` (a typo) blocks READY with `CONFIG_INVALID` instead of being ignored.

### 4.2b `autostart`: run AIO NAV without an operator

AIO NAV is fully automatic by default: it starts after power-on, is restarted after a failure,
and the automatic start never gives up.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Start AIO NAV and DSO (`nav.control_group`) by itself. Needs `nav.allow_control: true` (the shipped config sets it). `false` = Start / Stop by hand only. |
| `prerequisites` | `[imu]` | Readiness conditions (see 4.2a, e.g. `imu`, `gnss`) that must hold before a start, so AIO NAV does not start while the sensors are still coming up. |
| `prerequisite_stable_s` | `2.0` | How long the prerequisites must hold. |
| `prerequisite_timeout_s` | `120` | A prerequisite still missing after this is a **Fault** (for example *IMU data not available after 120 s*). It keeps waiting and starts as soon as the prerequisite appears. Waiting for the vehicle to be still or for the clock is never a fault. |
| `require_still` | `true` | Start only with the vehicle still (4.2a): AIO NAV's first alignment step needs it, and a start may follow a crash or a watchdog reboot while driving. |
| `still_stable_s` | `5.0` | How long the vehicle must be still before a start. |
| `start_services` | `[]` (shipped config: `[drivers]`) | Services with a `user_unit` to start if they do not run, while waiting to start. A unit an operator stopped is left alone, and nothing is started before the user's service manager answers (early in a power-on). `install.sh` then enables *lingering* for the service user, so the unit can start at power-on with nobody logged in. |
| `retry_backoff_s` | `[5, 15, 30, 60]` | Waits between attempts; the last one repeats. The count is kept for the power-on (a dashboard restart does not shorten the waits) and starts again after READY. |
| `max_attempts` | `0` | `0` = never give up. A number stops after that many attempts (*automatic start gave up*; **Start** tries again). |
| `stale_grace_s` | `10.0` | AIO NAV runs but its output stopped while its inputs are fine: it gets this long to recover, then it is stopped and started again. If an input is missing (no IMU data, no valid time) it is left running, because a restart would not help: *Waiting for …; AIO NAV may resume by itself*. |

Power-on runs BOOTING → CHECKING (sensor drivers started if needed, IMU data, the system clock
matching GNSS time (4.2d), the vehicle still) → NAV_STARTING (AIO NAV, then DSO) → KEEP_STILL →
MOVE_STRAIGHT → REFINING → READY (RTK float). Only AIO NAV decides whether a start worked: DSO
starts with it, and a DSO failure is handled by the DSO watchdog (4.7a), never by restarting AIO
NAV. An existing installation keeps its own config; compare it with `dashboard.yaml.new`, or
re-install with `--reset-config`.

Rules that always apply:

- **Stop** (Overview or Maintenance) puts the automatic start **on hold** until someone presses
  **Start** / **Resume**, or until the next power-on. A dashboard restart keeps the hold.
- **After READY**, if AIO NAV stops or its output stops, the vehicle is told **SAFE_STOP** at once;
  AIO NAV is started again only once the vehicle has been still for `still_stable_s`.
- AIO NAV that was running when the power went off is not a crash: the next power-on simply starts
  it again.
- A program that already runs (dashboard restart, desktop app) is adopted, never started twice.
- Only one program should own AIO NAV. With `autostart` on, an `aio-nav.service` that is
  enabled at boot is a **Fault** (`OWNERSHIP_CONFLICT`). With `autostart` on, `install.sh` does
  not enable that unit and disables it if it was; with only `nav.allow_control` it warns.

### 4.2c `status_udp`: the 0x05 status packet for the vehicle

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Send the readiness snapshot as a 42-byte 0x05 packet (format: [Operation manual, section 6.8](OPERATION.md#68-status-packet-0x05)). `false` = no 0x05. |
| `destination` | `auto` | IPv4 `address:port`. `auto` = the `output_udp` address **and port** of `aio_nav.yaml`: 0x05 arrives on the same socket as 0x04 and the receiver tells them apart by the message ID. Write an address only if the vehicle wants 0x05 on its own port. Host names are refused (no DNS lookups on the 5 Hz path), and so is the dashboard's own listener (`nav.udp_bind`). |
| `message_id` | `5` | 0x05 is reserved for this packet (AIO NAV only sends 0x04). |
| `rate_hz` | `2.0` | Packets per second; a change of state, action or inhibit mask is sent at once. It keeps sending after READY. |

The Maintenance Overview shows the readiness snapshot, the condition table, the automatic
start state and the packet counters (sent, errors). It says *Transmitting*, never *received*:
UDP has no acknowledgement. With the Wi-Fi hotspot on, the devices on it get copies (4.5a).

### 4.2d `gnss_time`: the system clock from GNSS time

The Jetson keeps no time over a power-off, and in the field there is no NTP. The dashboard gives
the IMU's GNSS time stamps to chrony as a time source (a shared-memory refclock that `install.sh`
sets up), so the system clock, the logs and AIO NAV's time agree.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Feed chrony, and wait for the clock before an automatic start. |
| `latency_ms` | `4.6` | Delay of the fastest IMU message after its stamp (measured over 8 h against NTP). |
| `shm_unit` | `2` | The chrony SHM unit (`refclock SHM 2` in `/etc/chrony/conf.d/aio-gnss.conf`). |
| `clock_tolerance_s` | `1.0` | The clock counts as matching GNSS time within this. |
| `clock_wait_s` | `120.0` | An automatic start waits at most this long for the clock (*Waiting for the system clock to match GNSS time*); then AIO NAV starts anyway and the event *System clock not corrected* is logged. |

### 4.2e `driver_watchdog`: restart the sensor drivers when the IMU goes silent

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Watch the sensor driver unit (Live mode only; in bag replay the IMU comes from the bag). |
| `service` | `auto` | A `services` entry with a `user_unit`; `auto` = the first one. |
| `stale_s` | `45.0` | The unit is active but its IMU sends nothing (or systemd gave up on it) for this long: it is restarted. |
| `retry_backoff_s` | `[60, 120, 300]` | Waits between restarts when it does not help; the last one repeats. |

An operator Stop of the drivers (Maintenance → ROS 2) is kept: the watchdog does not restart
them. Every restart is in the event log (*Sensor drivers restarted*).

### 4.3 `services`: systemd units to watch (and restart)

```yaml
services:
  aio_nav:   { label: AIO NAV, unit: aio-nav.service, process_pattern: lib/aio_nav_ros/aio_nav_node }
  camera:    { label: Camera driver, unit: camera.service, restartable: true }
  ouster:    { label: Ouster driver, unit: ouster.service, restartable: true }
  dashboard: { label: Dashboard, unit: aio-dashboard.service }
```

| Key | Meaning |
|-----|---------|
| *(entry name)* | Internal name, such as `camera`. `devices.<name>.service` and `nav.service` refer to it. |
| `label` | Name shown in the UI. |
| `unit` | systemd unit name (`systemctl status <unit>`). |
| `process_pattern` | Optional. Also counts the service as running when a process matches this text (`pgrep -f`). AIO NAV uses this, so starting it from the AIO Nav desktop app is detected too. |
| `restartable` | `true` adds a **Restart driver** button on the Camera or LiDAR page. Re-run the installer after changing this (see section 2). |
| `launch` | Name of a wrapper in the aio-nav-ros `install/aio_nav_ros/lib/aio_nav_ros/` folder (`aio-nav`, `aio-nav-dso`), or an absolute path. Lets the dashboard start and stop this program, which is found again by its `process_pattern`. The program runs as the dashboard's user in its own session; its ROS domain comes from `aio_nav.yaml`, not from the dashboard. Output goes to `logs/<name>.log`. The dashboard service uses `KillMode=process`, so AIO NAV keeps running when the dashboard restarts. |
| `optional` | `true` means the program being stopped is not a warning (used for DSO). |
| `user_unit` | Name of a systemd **user** unit (`systemctl --user`), such as `openrtk330-basler-drivers.service` (the camera and IMU/GNSS drivers). It adds a **Sensor drivers** panel with Start / Stop / Restart on **Maintenance > ROS 2**, so the live drivers can be stopped before playing a bag (otherwise live and recorded data mix). It runs as the dashboard's user, with no sudo. Stop and Restart ask for confirmation; the driver unit waits about 15 s before the drivers publish. |

A unit that does not exist on the device shows **Not installed** and raises no
warning, so unused entries are harmless. To find your real unit names:
```bash
systemctl list-units --type=service | grep -iE "camera|ouster|lidar|ros"
```
Only `unit` names from this file can be restarted. The web page cannot run any
other command.

### 4.4 `devices`: camera and LiDAR

```yaml
devices:
  camera:
    label: Camera
    host: ""
    service: camera
    topic_group: camera
    preview: { kind: image, topic: auto, max_width: 960 }
  lidar:
    label: LiDAR
    host: ""
    service: ouster
    topic_group: lidar
    preview: { kind: pointcloud, topic: auto, max_points: 30000 }
```

| Key | Meaning |
|-----|---------|
| *(entry name)* | Keep `camera` and `lidar`. The Maintenance view has pages for exactly these two names. |
| `label` | Name shown on lamps and pages. |
| `host` | Optional. IP address of the sensor, checked with ping every `network.ping_interval_s`. Empty = shown as **Not set up**, never pinged, no warnings. |
| `service` | Which `services` entry is this sensor's driver. |
| `topic_group` | Which `ros.topics` entries belong to this sensor (their `group`). |
| `preview.kind` | `image` for a camera, `pointcloud` for a LiDAR. Leave out `preview` to have no Live view. |
| `preview.topic` | ROS topic shown in **Live view**. `auto` = the first `CompressedImage` or `Image` topic (camera, skipping depth) or `PointCloud2` topic (LiDAR). Or give an exact name, e.g. `/ouster/points`. |
| `preview.hint` | Optional. Text the auto-picked topic name should contain, e.g. `color`. |
| `preview.max_width` | Camera: raw images are shrunk to this width before sending. Compressed images are sent unchanged. |
| `preview.max_points` | LiDAR: the point cloud is randomly thinned to this many points for the browser. |

The Live view reads the topic only while a Camera or LiDAR page is open, and stops 10 s
after you leave.

To find sensor IPs and topics:
```bash
ros2 topic list -t                 # topics with their message types
ros2 param get /ouster/os_driver sensor_hostname   # Ouster IP (node name may differ)
```

### 4.5 `network`

| Key | Default | Meaning |
|-----|---------|---------|
| `sensor_interface` | `auto` | Network interface connected to the sensor LAN, e.g. `eth0`. `auto` = the interface that routes to a device `host`, else the one with the default route. Shown as the **Network** lamp (link up or down). List interfaces with `ip -br link`. |
| `ping_interval_s` | `5` | Seconds between ping checks of device `host`s. |

### 4.5a `wifi`: Wi-Fi on the Settings page and the Maintenance page

The user **Settings** page (a network list, password, Connect) and **Maintenance → Network →
Wi-Fi** scan for networks and connect to one. Both use the same code and the same rules.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Show the Wi-Fi panel (when NetworkManager manages a Wi-Fi adapter). |
| `interface` | `auto` | The Wi-Fi adapter; `auto` = the first one. |
| `connect_timeout_s` | `30` | How long to wait for the network to accept the connection. |
| `ip_timeout_s` | `30` | How long to wait for an IPv4 address afterwards. |

Rules:

- **One-time setup, no root afterwards:** `sudo deploy/install.sh --with-wifi` installs a polkit
  rule that lets the service user (only) manage NetworkManager. Without it the page says so.
  This also allows that user to change wired profiles; the dashboard itself only ever touches
  Wi-Fi profiles. `uninstall.sh` removes the rule.
- **Only from a cable:** a change is accepted only when *your browser's* connection to the device
  goes over Ethernet (or you are on the device itself). From a browser on Wi-Fi or a VPN the
  page refuses, so a change can never cut off the page that made it.
- **Safe fallback:** if the new network fails (wrong password, no address within
  `ip_timeout_s`), the new profile is deleted and the previous Wi-Fi comes back.
- **Asks first** while navigation is Ready / Degraded, or when the vehicle output or the internet
  route (RTK corrections) goes over Wi-Fi.
- No password on the user Settings page (the cable rule above protects it); the maintenance
  password on the Maintenance page. Every change is recorded in the event log; the Wi-Fi password
  is never logged, shown or returned.
- A failed change that never left the current network does not drop it: the page says *Still on …*.

**Hotspot** (`wifi.hotspot`, installer part `hotspot`): on **Maintenance → Network** the Wi-Fi can
become a network of its own for phones and tablets instead of joining one (one radio cannot do
both). It is a NetworkManager profile named `aio-hotspot`: plain WPA2 (no WPA3 transition mode,
which some iPhones do not list), and it starts again at every power-on, ahead of the saved networks,
until it is stopped there. Phones open `http://10.42.0.1:8080`.

| Key | Default | Meaning |
|-----|---------|---------|
| `hotspot.enabled` | `auto` | `auto`: offered when the installer part `hotspot` is installed. `true` forces it on; anything else turns it off. |
| `hotspot.address` | `10.42.0.1/24` | The device's address on the hotspot; phones get addresses in this network. |
| `hotspot.band` | `bg` | `bg` = 2.4 GHz (works with every phone); `a` = 5 GHz. |
| `hotspot.allow_clients` | `true` | Add the hotspot network to `access.allowed_clients`, so phones may open the pages (only matters when that list is not empty). |
| `hotspot.relay_udp` | `true` | Send copies of the NAV (0x04) and status (0x05) packets to the devices on the hotspot. The switch on Maintenance → Network overrides this (kept in `state/hotspot_relay.json`). |
| `hotspot.relay_port` | `9000` | The UDP port on each device. |

- The hotspot name is typed on the page (default `AIO-NAV`), and so is the password (8–63
  characters) at each start. The password is kept only in the NetworkManager profile: never in the
  dashboard's files, logs or pages.
- **Start** asks first when the Wi-Fi is on a network (it leaves it); if the hotspot does not start,
  the previous network comes back. **Stop** asks first when devices are connected, then the Wi-Fi
  joins a saved network in range by itself; it does not come back after a power-off.
- Starting and stopping need the maintenance password and a browser on the cable (a phone on the
  hotspot cannot switch it off). The user Settings page shows the hotspot but cannot change it.
- The packet copies are unicast to each device that is associated now, found every 2 s (the
  vehicle's output is unchanged). 0x05 copies need `status_udp` on with a vehicle destination.
  The device count and the copies need the `iw` tool.

### 4.6 `system`: Jetson health

| Key | Default | Meaning |
|-----|---------|---------|
| `interval_s` | `2` | How often CPU, GPU, RAM, disk and temperature are read. |
| `disk_paths` | `["/"]` | Disks to report. Data folders are added automatically. |
| `disk_warn_percent` | `90` | Disk usage (%) that raises a "Storage almost full" warning. |
| `temp_warn_c` | `85` | Temperature (°C) that raises a "Temperature high" warning. |

### 4.7 `ros`: ROS 2 monitoring

```yaml
ros:
  enabled: true
  graph_interval_s: 2
  rate_window_s: 2
  nodes: [/aio_nav_node]
  topics:
    - { name: /nav/odometry, group: nav, label: NAV odometry, expected_hz: 100 }
    - { name: auto, type: sensor_msgs/msg/CameraInfo,  group: camera, label: Camera }
    - { name: auto, type: sensor_msgs/msg/PointCloud2, group: lidar,  label: LiDAR points, hint: ouster }
    - { name: auto, type: sensor_msgs/msg/Imu,         group: lidar,  label: Ouster IMU,   hint: ouster, strict: true }
```

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | `false` turns ROS 2 monitoring off. Topic lamps then show Unknown. |
| `mode` | `null` (shipped config: `live`) | The mode in use until someone picks another on **Maintenance > Replay** (that choice is stored in `state/ros_mode.json` and wins over this setting). Choosing a mode needs the optional replay module (4.12); without it this setting is the only one used, so a product machine stays on `live`. `null` keeps the environment of the service. |
| `modes` | `live`: `aio_nav.yaml`; `bag`: `aio_nav_bag.yaml` | Each mode names an aio-nav-ros config file (in the folder of the normal `aio_nav.yaml`). AIO NAV and DSO are started with that file as their argument, and its `ros_domain_id` / `ros_localhost_only` become the dashboard's own ROS environment, so all three always agree. The file also decides `use_sim_time`. A mode may add `domain_id` / `localhost_only` to override the file. A mode whose file is missing cannot be selected. Switching stops AIO NAV and DSO and restarts the dashboard (a few seconds); start AIO NAV again afterwards. The camera and IMU drivers are not touched. |
| `isolation_restart` | `false` (shipped config: `true`) | Restart the dashboard when its own ROS connection is stuck. This happened once after a boot: the dashboard started before the sensor drivers and never discovered them, so the IMU, camera and GNSS looked missing. It acts only when the sensor driver process is running **in the same ROS domain and localhost setting** as the dashboard, yet no other node is visible for `isolation_grace_s` (30 s). It never acts while the drivers are stopped or in another domain, and at most once per `isolation_cooldown_s` (600 s), so it cannot loop. The restart is recorded in the event log. |
| `sampling` | `enabled: false` (shipped config: `true`), `period_s: 5`, `window_s: 1.5`, `above_hz: 30` | Saves CPU on high-rate topics. A watched topic whose `expected_hz` is at least `above_hz` (the 100 Hz IMU) is listened to only `window_s` out of every `period_s`; the rate and freshness on the ROS 2 page are those of the last window and are marked *sampled*. Without it, a 100 Hz topic costs a Python callback per message (about 12% of one core). **Trade-off:** a topic that keeps its publisher but goes silent shows *Stale* up to `period_s` later (about 1 s without sampling), and a drop shorter than `period_s` can be missed. A topic that disappears is still *Missing* at once. It only changes this dashboard's own subscriptions: GNSS, the visual-odometry watch, the camera/LiDAR previews, the drivers and ROS itself are not affected. |
| `graph_interval_s` | `2` | How often the list of nodes and topics is refreshed. |
| `rate_window_s` | `2` | Time window used to measure each topic's rate. |
| `nodes` | `[]` | Node names that must exist (shown on the ROS 2 page; a missing one is a warning). |
| `topics` | `[]` | Topics whose rate and freshness are measured (see below). |

Each entry in `topics`:

| Field | Meaning |
|-------|---------|
| `name` | Exact topic name, or `auto` to pick one by `type`. |
| `type` | Message type, required with `auto`, e.g. `sensor_msgs/msg/PointCloud2`. |
| `hint` | With `auto`: prefer topics whose name contains this text. |
| `strict` | With `auto`: `true` = **only** accept topics containing `hint`. Use it so that, for example, "Ouster IMU" never picks the navigation IMU. |
| `group` | `nav`, `camera` or `lidar`. Ties the topic to a sensor page and its diagnostic. |
| `label` | Name shown in the UI. |
| `expected_hz` | Optional expected rate. Below half of it shows **Low rate**. It also sets how quickly the topic counts as stale. |

Topic states:

| State | Meaning | Warning? |
|-------|---------|----------|
| Active | Messages arriving at a normal rate | No |
| Low rate | Rate below 50% of `expected_hz` | Yes |
| Stale | Publisher exists, but no message for max(1 s, 5 ÷ `expected_hz`), or 3 s if no rate is set | Yes |
| Missing | A named topic does not exist or has no publisher | Yes |
| Not found | An `auto` topic matched nothing, so the sensor is probably not fitted | No |

Monitoring uses raw subscriptions, so messages are counted but never decoded. Prefer
light topics (for example `camera_info` rather than `image_raw`) for continuous
monitoring.

### 4.7a `dso_watchdog`: keep DSO healthy

```yaml
dso_watchdog:
  enabled: true
  topic: /dso/odometry
  service: dso
```

Only DSO is touched, never AIO NAV. Status is under Maintenance > ROS 2; there is nothing to
operate on the Overview page, and every action is in the event log.

* **Odometry turns NaN** (DSO lost tracking): DSO is restarted **at once**.
* **DSO is not running but should be** (it crashed, or failed right after a start): it is started
  again, first after `retry_s` (30 s), then after waits that double up to `retry_max_s` (300 s);
  once it has run for `stay_up_s` the waits start over. It never gives up. A start from before
  this power-on does not count as "should be running".
* **DSO uses too much memory** (RAM + swap above `max_memory_mb`): DSO is restarted **at once**, so
  a runaway DSO cannot fill the memory and hang the whole machine.
* A DSO you stopped on purpose stays stopped. "Should be running" is recorded when AIO NAV and DSO
  are started or stopped from the dashboard (in `state/processes.json`), and a DSO that is running
  counts as wanted.
* If NaN keeps coming back (`fast_restarts` restarts within `window_s`), further restarts are also
  spaced `retry_s` apart, so a bad camera view does not cause a restart loop.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` (shipped config: `true`) | Turn the watchdog on. |
| `topic` | `/dso/odometry` | `nav_msgs/Odometry` topic to watch. Any NaN or infinite pose or velocity counts as bad. |
| `service` | `dso` | The `services` entry (with a `launch` setting) to restart or start. |
| `bad_messages` | `1` | NaN messages in a row before restarting (1 = at once). |
| `cooldown_s` | `2` | Minimum gap between two NaN restarts. |
| `settle_s` | `3` | After a restart, odometry is ignored for this long. |
| `retry_s` | `30` | First wait before starting a DSO that is not running, and the gap between restarts when NaN keeps coming back. |
| `retry_max_s` | `300` | The wait doubles while DSO keeps exiting at once, up to this. |
| `stay_up_s` | `120` | Running this long resets the waits. |
| `fast_restarts` / `window_s` | `3` / `60` | See above. |
| `max_memory_mb` | `auto` | DSO's RAM plus swap above this restarts DSO at once (AIO NAV is not touched). `auto`: 20% of RAM (about 3.1 GB on a 16 GB Orin NX); `0`: off. Added after DSO kept every camera frame (about 25 MB/s) until RAM and swap were full and the whole machine hung. |

### 4.7b `gnss`: GNSS lamp

```yaml
gnss:
  topic: /openrtk330/gnss/fix
  timeout_s: 3
```

The GNSS lamp shows the receiver's position quality, taken from `NavSatFix.status.status`
(checked against a recorded drive): **green** RTK fix (status 2, about 3 cm), **yellow** RTK float
(status 1, about 10 cm), **red** SPP (status 0, 10 m). The driver also sends status -1 *with* a
valid position (about 10 cm, single epochs inside RTK float periods), so a message with a usable
position (finite latitude/longitude and a known covariance) is never "No signal": with an unrated
status it shows **Fix (type unknown)**, amber when better than 1 m, red otherwise. **No signal** means
no usable position: no message for `timeout_s`, or a status without a position; the advisory then
says which (for example *no GNSS message for 3 h*, i.e. the receiver driver sends nothing). It is
advisory and never changes Ready. It is not the same as the "GNSS" aiding lamp on
the Navigation page, which shows whether the navigation filter used GNSS in the last second.

### 4.8 `data`: the Data (download) page

```yaml
data:
  roots: auto
```

`auto` offers AIO NAV's log folder. That is the folder of `fusion_txt_path` in
`aio_nav.yaml` if it is an absolute path, otherwise `output/` next to aio-nav-ros's
`install/` folder. To list folders yourself:

```yaml
data:
  roots:
    - { id: logs,  name: AIO NAV logs, path: /home/jetson/aio-nav-ros/output }
    - { id: bags,  name: ROS bags,     path: /home/jetson/bags }
```

| Field | Meaning |
|-------|---------|
| `id` | Short unique name, used in download links. |
| `name` | Name shown on the Data page. |
| `path` | Folder to offer. Downloads cannot leave this folder. The page is read-only: nothing can be uploaded, deleted or renamed. |

### 4.9 `events`

| Key | Default | Meaning |
|-----|---------|---------|
| `log_file` | `logs/events.jsonl` | File that keeps state changes, diagnostics and restarts. Rotated at 5 MB. |
| `max_memory` | `500` | How many recent events the Diagnostics page can show. |

### 4.10 `actions`

| Key | Default | Meaning |
|-----|---------|---------|
| `restart_timeout_s` | `30` | A driver restart that takes longer is reported as failed. |
| `diagnostic_timeout_s` | `15` | Same, for **Run diagnostic**. |
| `stop_grace_s` | `5` | Stopping AIO NAV or DSO sends SIGTERM, then SIGKILL after this many seconds. |

### 4.11 `fake`: demo / development only

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | `true` uses simulated services, network and ROS data from `state_file` instead of the real ones. **Keep `false` on a real device.** |
| `state_file` | `dev/fake_state.json` | The simulated state, editable while running. |

---

### 4.12 `modules`: optional modules

Features that not every machine needs are separate modules. A module is **not installed by
default**; it is added with an installer flag and is then on, unless switched off here.

```yaml
modules:
  replay:
    bag_roots: ["~", /media/jetson/data]
```

**`replay`**: the Live / Bag replay switch and the bag player on **Maintenance > Replay**
(OPERATION section 4.6). Install with `sudo deploy/install.sh --user <you> --with-replay`.
Without it the machine always runs Live.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | `false` hides the module although it is installed (the machine then runs Live). |
| `bag_roots` | `["~"]` | Folders searched for bags. A folder holding a `metadata.yaml` is a bag. `~` is the service user's home folder. Only bags found here can be played. |
| `scan_depth` | `2` | How many folder levels below each root are searched. The search does not go into a bag or into hidden folders. |
| `ros_setup` | `auto` | The `setup.bash` sourced before `ros2 bag play`. `auto`: the aio-nav-ros `install/setup.bash` (it also knows AIO NAV's own message types), else `/opt/ros/<distro>/setup.bash`. |
| `log_file` | `logs/bag_play.log` | Output of the player (shown under *Player output*). |
| `stop_grace_s` | `5` | Stop sends SIGINT, then SIGTERM, then SIGKILL; this is the wait after SIGINT. |

The player state, presets and the last settings per bag are kept in `state/bag_play.json` and
`state/bag_presets.json`.

## 5. Common tasks

**Add the camera and LiDAR IP addresses.** In the existing `devices` section, fill in
the `host` lines and leave the other lines as they are:
```yaml
devices:
  camera:
    label: Camera
    host: 192.0.2.10
    ...
  lidar:
    label: LiDAR
    host: 192.0.2.20
    ...
```

**Show a specific camera stream in Live view.** In `devices.camera`, change the `preview` line:
```yaml
    preview: { kind: image, topic: /camera/color/image_raw/compressed, max_width: 960 }
```

**Use your own driver unit names, so Restart works.** Change the `camera` and `ouster` lines in `services`:
```yaml
services:
  camera: { label: Camera driver, unit: usb-cam.service, restartable: true }
  ouster: { label: Ouster driver, unit: ouster-driver.service, restartable: true }
```
Then re-run `sudo deploy/install.sh --user jetson`.

**Ready needs all three alignment lamps by default** (Alignment, Initial heading, Fine alignment). To relax it, change the `ready_requires` line in `nav`:
```yaml
nav:
  ready_requires: [alignment, heading_valid, fine_alignment]
```

**aio-nav-ros is installed somewhere unusual.** Change the `aio_nav_config` line in `nav`:
```yaml
nav:
  aio_nav_config: /data/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml
```

After any change: `sudo systemctl restart aio-dashboard`.

## 6. Troubleshooting

| Symptom | Likely cause and fix |
|---------|----------------------|
| My change has no effect | You edited `~/web_monitor/config/dashboard.yaml` instead of `/opt/aio-dashboard/config/dashboard.yaml`, or forgot `sudo systemctl restart aio-dashboard`. |
| Fault *Settings file has an error* after editing | YAML syntax error: the dashboard runs with the last good settings. Run `aio-dashboard check` (section 3), fix the file, restart. |
| Fault *Settings with an invalid value: defaults used* | A value of the wrong kind; `journalctl -u aio-dashboard -n 40` names the key. |
| "Sending to" shows the wrong address | It comes from `output_udp` in `aio_nav.yaml`, not this file (see 4.2). |
| Network page says `aio_nav.yaml` not found | Set `nav.aio_nav_config` to the full path (see section 5). |
| ROS 2 page says rclpy is not available | ROS 2 Humble isn't at `/opt/ros/humble`, so the service could not load it. |
| Live view: "no PointCloud2 topic found" | No matching topic is published. Check `ros2 topic list -t`, or set `preview.topic`. |
| Camera/LiDAR shows "Not set up" | No `host` is set. This is normal if you don't need ping checks. |
| Restart driver fails with a sudo error | The unit name changed after install. Re-run the installer (section 2). |
