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

If the file has a syntax error, the dashboard cannot start, and systemd retries every
few seconds. Check the file with:
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

The product view is for operators. The maintenance view is for engineers and can
restart drivers. To keep the maintenance view reachable only from the Jetson itself,
set `maintenance.host: 127.0.0.1`.

### 4.1a `access`: who may open the pages

```yaml
access:
  allowed_clients: [192.168.116.154]
```

| Key | Default | Meaning |
|-----|---------|---------|
| `allowed_clients` | `[]` | IP addresses or CIDRs (e.g. `192.168.116.0/24`) of the computers allowed to open the product and maintenance views. Anyone else gets `403 Forbidden`. `127.0.0.1` (this Jetson) is always allowed. Empty = no restriction. |

The shipped value is the external computer on the sensor LAN (`192.168.116.154`); this
Jetson is `192.168.116.1` on `eno1`. It is an access filter, not authentication: it
does not protect against a computer that spoofs that address on the same LAN.

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
  again **every `retry_s` (30 s)** until it stays up.
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
| `retry_s` | `30` | Interval between tries when DSO is not running, and between restarts when NaN keeps coming back. |
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
(status 1, about 10 cm), **red** SPP (status 0, 10 m) or no signal (status -1, or no message for
`timeout_s`). It is advisory and never changes Ready. It is not the same as the "GNSS" aiding lamp on
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
| Page doesn't load after editing | YAML syntax error. Run the check in section 3 and read `journalctl -u aio-dashboard -n 40`. |
| "Sending to" shows the wrong address | It comes from `output_udp` in `aio_nav.yaml`, not this file (see 4.2). |
| Network page says `aio_nav.yaml` not found | Set `nav.aio_nav_config` to the full path (see section 5). |
| ROS 2 page says rclpy is not available | ROS 2 Humble isn't at `/opt/ros/humble`, so the service could not load it. |
| Live view: "no PointCloud2 topic found" | No matching topic is published. Check `ros2 topic list -t`, or set `preview.topic`. |
| Camera/LiDAR shows "Not set up" | No `host` is set. This is normal if you don't need ping checks. |
| Restart driver fails with a sudo error | The unit name changed after install. Re-run the installer (section 2). |
