# AIO System Dashboard v1

Web dashboard for the Jetson AIO navigation system.

| UI | Port | Audience | Pages |
|----|------|----------|-------|
| Product | `:8080` | Operators (no login) | Overview (status and its steps: sensors, GNSS / RTK, AIO NAV, alignment, recording, storage, vehicle link where a vehicle is connected) · Navigation · Data · Setup (lever arms) · Settings (Wi-Fi, environment: network / RTK / vehicle, automatic start, vehicle link), with Start / Restart / Stop / Resume of AIO NAV; English / 繁體中文 |
| Maintenance | `:8081` | Engineers (password) | Overview · System · ROS 2 · GNSS / RTK · Camera · LiDAR · Network (Wi-Fi, hotspot) · Diagnostics · Still detection · AIO NAV config · Terminal (· Replay with the replay part) |

The main data source is the **AIO NAV 0x04 binary packet** that `aio_nav_node`
(`../aio-nav-ros`) always sends to `127.0.0.1:9000`. With the shipped settings the system runs
without an operator:

- **Automatic start:** AIO NAV + DSO start after power-on (sensor drivers first) and are started
  again after a failure (after READY only with the vehicle still). Needs `nav.allow_control`.
- **0x05 status packet:** the vehicle is told, twice a second, whether it may use the navigation
  output (READY with no inhibit bit), next to the 0x04 data.
- **Watchers:** DSO watchdog (NaN, crash, memory), sensor-driver watchdog (IMU silent), ROS
  isolation guard (the dashboard's own ROS connection stuck), systemd watchdog. See
  [FLOWS 7](docs/FLOWS.md#7-who-restarts-what).
- **System clock from GNSS time** (chrony), so time stamps are right in the field without NTP.

AIO NAV and DSO run in their own sessions, so a dashboard crash or restart never stops them.


**Manuals:** operation, alignment SOP and UDP output format:
[English](docs/OPERATION.md) · [繁體中文](docs/OPERATION.zh-TW.md).
Settings: [English](docs/CONFIG.md) · [繁體中文](docs/CONFIG.zh-TW.md).
Optional system recorder (a black box for the computer, to trace freezes and power cuts; install with `sudo deploy/install.sh --with-sysmon`): [English](docs/SYSMON.md) · [繁體中文](docs/SYSMON.zh-TW.md).
What the features are and how they relate (which ones exclude each other, which overlap), in Traditional Chinese: [docs/FEATURES.md](docs/FEATURES.md).
Engineering flowcharts (system data flow, health tick, readiness states, automatic start, Wi-Fi, vehicle side): [docs/FLOWS.md](docs/FLOWS.md).
繁體中文 terminology, shared word for word with the vehicle-side aio-nav-receiver: [docs/TERMINOLOGY.zh-TW.md](docs/TERMINOLOGY.zh-TW.md).
Ideas discussed and kept for later (in Traditional Chinese): [docs/IDEAS.md](docs/IDEAS.md).
The original v1 specification is kept for history only, no longer up to date: [docs/history/v1-spec.md](docs/history/v1-spec.md).

## Architecture

```
aio_system_dashboard/
  __main__.py          one process, shared state, two HTTP servers (:8080, :8081)
  config.py            config/dashboard.yaml + the active aio-nav-ros config file (re-read when it changes)
  modules.py           optional modules (packages installed only with --with-<name>; today: replay)
  nav/                 decoder.py (0x04, with the VUPT + velocity_up fixes), trajectory.py (last 60 s
                       @10 Hz + older @1 Hz)
  collectors/          nav_udp (raw rate), system, services, network, ros2 (sampled), gnss_monitor
                       (GNSS lamp), gnss_diag (GNSS / RTK page), gnss_time (clock -> chrony), speed,
                       recording (is AIO NAV writing its logs), dso_watchdog, ros_guard, hotspot_relay, fake
  media/               on-demand camera image / point-cloud preview (tap.py), decoders, fake frames
  state/               health.py (5 Hz evaluator: product state, alignment step, advisories, events),
                       readiness.py (states, actions and reasons, by name), environment.py
                       (network / RTK / vehicle, detected; decides whether READY needs RTK), still.py
                       (vehicle still detection), indicator.py (debounced lamps), store.py
  actions/             fixed actions only: process_control (AIO NAV / DSO), service_control (drivers),
                       autostart, driver_watchdog, diagnostics, aionav_config (config editor),
                       terminal (shell sessions), wifi (NetworkManager, with rollback; hotspot)
  data_access/         read-only browse/download inside configured roots
  web/                 Flask apps: product.py, setup.py (lever arms), maintenance.py, still.py,
                       tools.py (editor, terminal), auth.py, access.py, settings_api.py (Wi-Fi / environment on
                       both apps), server.py, i18n.py + i18n_zh.py (user-page languages)
  templates/, static/  Bootstrap + Alpine.js + Leaflet + xterm.js (vendored, offline)
  vehicle/             output to the vehicle, a layer the core never imports: output.py (listens to the
                       health step), protocol.py (state / action codes, bit numbers), packet.py (0x05),
                       sender.py (UDP), gate.py (reference receiver for the vehicle side)
aio_dashboard_replay/  optional module: Live / Bag replay switch and bag player
aio_sysmon/            optional system recorder (separate service)
```

Packet processing runs at the raw rate (100 Hz). Health is evaluated at 5 Hz. The browser
polls `/api/state` at 5 Hz and the trajectory every 2 s.

### Health model

Two layers, computed together every 0.2 s (`state/health.py`, `state/readiness.py`):

| Page state | Condition |
|------------|-----------|
| **STOPPED** | AIO NAV not running and nobody asked it to run (normal, not a fault) |
| **STARTING** | asked to start, or running without output yet (up to `startup_timeout_s`, 60 s); also while the automatic start waits |
| **INITIALIZING** | packets fresh but `ready_requires` flags (default `alignment`, `heading_valid`, `fine_alignment`) not all set; reports the current step and what to do |
| **READY** | process up, packets fresh, alignment complete, and the required conditions held: RTK where the environment has RTK (detected by `state/environment.py`, or set on a page; `readiness.require_rtk`), plus anything in `readiness.require` |
| **DEGRADED** | was READY in this AIO NAV run, and a required condition (such as RTK) or the alignment was lost |
| **FAULT** | AIO NAV stopped although it should run, no output after the start-up time, output stopped for `stale_fault_s` (2 s) or frozen, the UDP listener failed, AIO NAV running twice, or the automatic start gave up |

For the vehicle the same snapshot gives a finer state (BOOTING, CHECKING, NAV_STARTING, KEEP_STILL,
MOVE_STRAIGHT, REFINING, READY, DEGRADED, FAULT, MAINTENANCE), a requested action (HOLD_STILL,
SAFE_STOP, …) and inhibit bits; they go out in the 0x05 packet
([operation 6.8](docs/OPERATION.md), [FLOWS 3](docs/FLOWS.md#3-readiness-states)).

The user pages list the steps under the status from the same snapshot (`_startup_checks`):
Sensors, GNSS / RTK, AIO NAV, Alignment, Recording (AIO NAV's log folder written within 5 s; red
when navigation is in use and nothing is written), Storage (free space of the data disk, amber at
`system.disk_warn_percent`, red when full), and Vehicle link only where a vehicle is connected
(the environment). Grey means not used here or not started; without a vehicle, READY reads
*Recording, data usable* ([operation 3.2](docs/OPERATION.md#32-start-up-progress)).

Camera/LiDAR not connected, sensor-LAN link down and low disk are **advisories**: they never
change READY. GNSS is an advisory too where RTK is not required (no RTK detected here, or *No*
chosen for RTK on the Settings page). A state gets worse
after about 0.5 s and recovers after about 2 s, so a single missed sample does not flicker.

"UDP Output: Streaming" means `aio_nav_node` is sending packets. UDP has no ACK,
so the dashboard never claims that the remote receiver got the data.

### Decoder fixes compared with setup_env

- After the wire bit reversal, wire flag bit 3 is **VUPT** (odometry update), not `imu_valid`.
- The third velocity on the wire is **Up** (`-vel_d`). It is exposed as `velocity_up`.
- The packet is 156 bytes: 5 header, 150 payload `<Qdd28fH3f`, 1 checksum.

### Live view (Maintenance → Camera / LiDAR)

The Camera and LiDAR pages each have a view-only live preview of the ROS topic set in
`devices.<name>.preview`:

- **Camera** (`kind: image`): about 2 frames/s. `sensor_msgs/CompressedImage` is passed through unchanged.
  `sensor_msgs/Image` is downscaled to `max_width` and sent as JPEG (with OpenCV) or PNG.
- **LiDAR** (`kind: pointcloud`): `sensor_msgs/PointCloud2` is randomly downsampled to `max_points`
  and drawn on a canvas, with a top view or a tilted 3D view (drag to rotate, scroll to zoom),
  colored by height or intensity.

The dashboard subscribes only while a page is requesting frames, and unsubscribes
10 s after the last request. Messages are received without deserialization; only the
latest one is converted, at the rate the browser asks for it. This keeps the dashboard
from being a permanent consumer of heavy sensor streams. The preview needs numpy, which ships with ROS 2.

## Development (x86, no ROS, no sensors)

```bash
python3 -m pip install --target .devdeps flask pyyaml psutil   # once (no venv needed)
tools/dev.sh start all        # dashboard (fake mode) + fake NAV sender cycling all scenarios
# open http://127.0.0.1:8080 and http://127.0.0.1:8081
tools/dev.sh stop
PYTHONPATH=.devdeps python3 -m unittest discover -s tests -t .
```

- `tools/fake_nav_sender.py --scenario NAME` sends real 0x04 packets. The scenarios are `normal`, `align`,
  `gnss_loss`, `flags`, `pause`, `loss`, `heading_wrap`, `restart` and `all`.
- `dev/fake_state.json` is hot-reloaded. Edit it while the dashboard runs to stop services,
  make devices unreachable, mark ROS topics as `low_rate` / `stale` / `missing`, or change the
  `aio_nav` PID to simulate a node restart.
- `config/dashboard.dev.yaml` is the dev config. Set `network.sensor_interface` to a real NIC on your machine.
- Templates are cached, so restart the dashboard after editing them (`tools/dev.sh start`).

## Deployment (Jetson, offline)

### Release package without source code (recommended for devices)

Build once on an Orin (same CPU architecture and Python version as the target devices):

```bash
sudo apt install -y gcc python3-dev && python3 -m pip install --user nuitka   # build machine only
tools/build_release.sh            # -> dist/aio-dashboard-<version>-aarch64-py3.10.tar.gz
```

The backend is compiled with Nuitka into one native module (`aio_system_dashboard*.so`).
The package contains no `.py` files. The HTML, JS and CSS stay readable, because browsers
download them anyway. Copy the package to each device, then:

```bash
tar xzf aio-dashboard-*.tar.gz && cd aio-dashboard-*/
sudo ./install.sh --user jetson
cd .. && rm -rf aio-dashboard-*          # nothing with source code is left on the device
```

Installing a release over a source install removes the old `.py` files from
`/opt/aio-dashboard`. Day-to-day control uses the installed `aio-dashboard` command:

| Command | Does |
|---------|------|
| `aio-dashboard status` | service state and web addresses |
| `aio-dashboard start` / `stop` / `restart` | control the service |
| `aio-dashboard logs` | follow the log |
| `aio-dashboard config` | edit settings, check them, offer a restart |
| `aio-dashboard check` | check the settings file for mistakes |
| `aio-dashboard version` | installed version |
| `aio-dashboard password` | set the maintenance view password (no default; it stays locked until set) |

### From a source checkout

```bash
sudo deploy/install.sh --user nvidia
# optional: also run aio_nav_node as a systemd service
sudo deploy/install.sh --user nvidia \
  --with-aio-nav /home/nvidia/aio-nav-ros/install/aio_nav_ros/lib/aio_nav_ros/aio-nav
```

**Optional parts.** The first run (or any run with `--choose`) shows a menu of the optional parts;
type their numbers to switch them on or off, Enter to continue, `q` to quit without changes:

| Part | Menu title | What it adds |
|------|------------|--------------|
| `sysmon` | Black-box recorder (aio-sysmon) | `aio-sysmon.service`, `/var/log/aio-sysmon`, a persistent journal (300 MB), the `aio-sysmon` command ([docs/SYSMON.md](docs/SYSMON.md)) |
| `replay` | Replay: Live / Bag switch and bag player | Maintenance > Replay ([operation 4.6](docs/OPERATION.md)) |
| `wifi` | Wi-Fi from the pages | a polkit rule so the service user can manage NetworkManager (Settings page and Maintenance > Network) |
| `hotspot` | Wi-Fi hotspot (Maintenance > Network) | the right to share the Wi-Fi as a hotspot, and a dispatcher script that keeps it a closed network (nothing forwarded); needs `wifi` (choosing it switches `wifi` on), `dnsmasq` and nft or iptables |

The choice is saved in `/opt/aio-dashboard/config/installed-options`, and later runs install the
same parts without asking. A part switched off is removed (the sysmon records in
`/var/log/aio-sysmon` are kept; switching off `hotspot` deletes the hotspot profile). Without a
terminal, the first run keeps the parts already on the machine (none on a fresh install); use
`--with-<part>` there.

| Option | Does |
|--------|------|
| `--choose` | show the menu again |
| `--list` | what is installed and chosen (no sudo needed) |
| `--dry-run` | what an install would add or remove, then stop (no sudo needed) |
| `--with-<part>` / `--without-<part>` | switch a part on or off without the menu (`sysmon`, `replay`, `wifi`, `hotspot`) |
| `--yes`, `-y` | never ask (for scripts) |
| `--user U` | the service user (default: the user running sudo) |
| `--prefix P` | install folder (default `/opt/aio-dashboard`) |
| `--with-aio-nav PATH` | also install `aio-nav.service` (not enabled at boot while the dashboard starts AIO NAV itself) |
| `--reset-config` | replace the installed config with the shipped one (the old one is kept as `.bak`) |

`install.sh` does the following:
- copies the app to `/opt/aio-dashboard`, keeping an existing config
- installs the bundled aarch64/cp310 wheels from `wheels/` (pip if present, otherwise it unpacks them)
- installs and starts `aio-dashboard.service`, which runs as the ROS user with ROS 2 Humble sourced so `rclpy` works.
  The unit has a systemd watchdog (`WatchdogSec=60`: a dashboard that stops answering is restarted)
  and keeps running when the kernel kills AIO NAV or DSO for lack of memory
- writes `/etc/sudoers.d/aio-dashboard`, which allows only `systemctl restart <unit>` for units with `restartable: true`
- sets the system clock from GNSS time: a chrony time source (`/etc/chrony/conf.d/aio-gnss.conf`, refclock SHM 2)
  fed from the IMU time stamps, so the clock is right in the field without NTP (if chrony is installed)
- reboots the device 10 s after a kernel panic (`/etc/sysctl.d/90-aio-unattended.conf`, `kernel.panic = 10`)
  instead of waiting for the hardware watchdog
- enables *lingering* for the service user when the automatic start must start the sensor drivers (a user unit) at power-on
- installs the optional parts chosen above
- reminds you to run `aio-dashboard password` if no maintenance password is set

`sudo deploy/uninstall.sh` (`--prefix P`, `--backup-dir D`) first saves `config/` and `state/` to `~/aio-dashboard-backup-<time>`, then
removes the units, the sudoers, sysctl, chrony and polkit files, the hotspot profile and
`/opt/aio-dashboard`. It leaves the sysmon records, the persistent journal setting and lingering.

No configuration is required. For every setting and how to change it, see the configuration
manual: [English](docs/CONFIG.md) · [繁體中文](docs/CONFIG.zh-TW.md). At start-up the dashboard detects:

| Setting | `auto` behaviour |
|---------|------------------|
| `nav.aio_nav_config` | newest `aio_nav.yaml` under `/home/*/aio-nav-ros/install/...` or `~/.local/opt/aio-nav-ros/...` (or `$AIO_NAV_CONFIG`) |
| `data.roots` | folder of an absolute `fusion_txt_path`, else `output/` next to aio-nav-ros `install/` |
| `network.sensor_interface` | interface that routes to the sensors, else the default-route interface |
| ROS topics, Live view topics | first topic of the right message type (`hint` prefers a name, `strict` requires it) |

Optional settings stay quiet when they are empty:
- A device without a `host` shows "Not set up" and is never pinged.
- A driver unit that doesn't exist shows "Not installed" and raises no warning.
- An auto topic that finds nothing shows "Not found".

To pin anything down, edit `/opt/aio-dashboard/config/dashboard.yaml`, then run
`sudo systemctl restart aio-dashboard`. Re-running `install.sh` keeps your config. Add
`--reset-config` to replace it with the shipped default; the old one is saved as `.bak`.
Re-run `install.sh` after changing which units are restartable.

## Security notes (closed sensor LAN)

- The maintenance view needs a password (`aio-dashboard password`; salted PBKDF2 hash, no default).
  Sessions are HttpOnly / SameSite=Strict cookies bound to the client IP and end after 15 minutes
  without use; repeated wrong passwords lock out briefly. `maintenance.lock: tools` leaves the
  pages open to view and asks for the password only for changes (config editor, terminal, still
  detection, Wi-Fi / hotspot, environment: RTK and vehicle).
- The **Terminal** page is a real shell of the service user. Choose a strong password, and keep
  `access.allowed_clients` to the computers that need it.
- Elsewhere no command strings are accepted: actions are fixed IDs, each target in a whitelist built from config.
- Restarts need confirmation in the UI, have a timeout, and are logged (Diagnostics page and `logs/events.jsonl`), as are sign-ins, config saves and terminals.
- POSTs require JSON and the header `X-Requested-With: aio-dashboard`, which blocks plain cross-site form posts.
- Downloads are limited to the configured roots: realpath containment rejects `..`, absolute paths and escaping symlinks. There is no upload, delete or rename.
- Each port serves only its own routes. Maintenance APIs are not reachable on :8080. The product view has no login;
  its only changes are Start / Stop / Resume, the lever arms on Setup (written to the Live `aio_nav.yaml` after a test
  start, with a backup), Wi-Fi (accepted only from a browser connected by cable, so a change cannot cut off the page
  that made it) and the environment (RTK, vehicle; saying *No RTK here* needs a confirmation). All are logged.

## Not included

Offline map tiles, trend charts (except the GNSS page's last 10 minutes), deep IMU/camera/LiDAR
diagnostics, ROS message browsing, and recording. The shipped config has no LiDAR; add a `lidar`
device to use the LiDAR page.
Ideas kept for later are in [docs/IDEAS.md](docs/IDEAS.md).

## Optional parts

Features that not every machine needs are **optional parts** of the installer. They are not
installed by default and the dashboard works without them. They are switched on in different ways:

| Part | Installer | What it installs | Config switch | Release package |
|------|-----------|------------------|---------------|-----------------|
| `replay` | `--with-replay` | the `aio_dashboard_replay` module: Maintenance > Replay ([operation 4.6](docs/OPERATION.md)); without it the machine runs Live only | `modules.replay.enabled` | only when built with `tools/build_release.sh --with-replay` |
| `sysmon` | `--with-sysmon` | the system recorder: its own service, `/var/log/aio-sysmon`, persistent journal ([SYSMON](docs/SYSMON.md)) | none (it is its own service) | never: install from a source checkout |
| `wifi` | `--with-wifi` | only the polkit rule that lets the service user change Wi-Fi; the Wi-Fi code is always in the dashboard | `wifi.enabled` (shows the panel; without the part, changes are refused) | code always included |
| `hotspot` | `--with-hotspot` | the right to share the Wi-Fi as a hotspot and a dispatcher that keeps it closed; copies of the NAV and status packets for the devices on it (needs `wifi`, `dnsmasq`, nft or iptables) | `wifi.hotspot.enabled: auto` (= installed) | code always included |

The installer's menu (see Deployment) chooses the same parts. Details: [CONFIG](docs/CONFIG.md).

A **module** is the kind of part that is a separate Python package next to
`aio_system_dashboard` (today only `aio_dashboard_replay/`), with `NAME`, `TITLE`, `DEFAULTS` and
`register(ctx, app)`, listed in `aio_system_dashboard/modules.py`. The core never imports it
directly: it only asks `modules.py` whether the module is installed and on (`modules.<name>.enabled`),
and the module adds its own Maintenance page, routes, templates and static files.
`deploy/install.sh --with-<name>` copies it, and `tools/build_release.sh --with-<name>` compiles it
into a release.

New optional features follow the module pattern where they can (own folder, own flag, own
`enabled` setting) and are developed on a short-lived `feature/<name>` branch that is merged into `main`.
