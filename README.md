# AIO System Dashboard v1

Web dashboard for the Jetson AIO navigation system, implementing
[`docs/AIO System Dashboard v1 — Implementation Specification.md`](docs/).

| UI | Port | Audience | Pages |
|----|------|----------|-------|
| Product | `:8080` | Operators (no login, read-only) | Overview · Navigation · Data |
| Maintenance | `:8081` | Engineers (closed LAN, no login) | Overview · System · ROS 2 · Camera · LiDAR · Network · Diagnostics |

The main data source is the **AIO NAV 0x04 binary packet** that `aio_nav_node`
(`../aio-nav-ros`) always sends to `127.0.0.1:9000`. The dashboard only monitors
the system. It does not start navigation or sensor processes, so a dashboard crash never stops them.

## Architecture

```
aio_system_dashboard/
  __main__.py          one process, shared state, two HTTP servers (:8080, :8081)
  config.py            config/dashboard.yaml + read-only output_udp/output_rate from aio_nav.yaml
  nav/decoder.py       0x04 packet decoder (from setup_env, with the VUPT + velocity_up fixes)
  nav/trajectory.py    session trajectory: last 60 s @10 Hz + older @1 Hz, incremental fetch
  collectors/          read-only: nav_udp (raw rate), system, services, network, ros2, fake
  media/               on-demand camera image / point-cloud preview (tap.py), decoders, fake frames
  state/indicator.py   debounced status (raise/clear hold, no flicker on a single miss)
  state/health.py      5 Hz evaluator → READY / INITIALIZING / FAULT, advisories, issues, events
  actions/             whitelisted actions only: run_diagnostic(device), restart_service(unit)
  data_access/         read-only browse/download inside configured roots
  web/                 Flask apps (product.py, maintenance.py)
  templates/, static/  Bootstrap + Alpine.js + Leaflet (vendored, offline)
```

Packet processing runs at the raw rate (100 Hz). Health is evaluated at 5 Hz. The browser
polls `/api/state` at 5 Hz and the trajectory every 2 s.

### Health model

| State | Condition |
|-------|-----------|
| **FAULT** | AIO NAV process/service down, or no NAV packet for `stale_fault_s` (2 s) |
| **INITIALIZING** | Packets fresh but `ready_requires` flags (default `alignment`, `heading_valid`) not all set |
| **READY** | Process up, packets fresh, alignment complete |

GNSS loss, camera/LiDAR not connected, sensor-LAN link down and low disk are
**advisories**: they never change READY. A state only changes after the new condition has held
for a while (fault about 0.5–1 s, recovery about 2 s).

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
from being a permanent consumer of heavy sensor streams (spec §15). The preview needs numpy, which ships with ROS 2.

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

### From a source checkout

```bash
sudo deploy/install.sh --user nvidia
# optional: also run aio_nav_node as a systemd service
sudo deploy/install.sh --user nvidia \
  --with-aio-nav /home/nvidia/aio-nav-ros/install/aio_nav_ros/lib/aio_nav_ros/aio-nav
```

`install.sh` does the following:
- copies the app to `/opt/aio-dashboard`, keeping an existing config
- installs the bundled aarch64/cp310 wheels from `wheels/` (pip if present, otherwise it unpacks them)
- installs and starts `aio-dashboard.service`, which runs as the ROS user with ROS 2 Humble sourced so `rclpy` works
- writes `/etc/sudoers.d/aio-dashboard`, which allows only `systemctl restart <unit>` for units with `restartable: true`

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

## Security notes (closed sensor LAN, spec §4)

- There is no shell access, and no command strings are accepted. Actions are fixed IDs, and each target must be in a whitelist built from config.
- Restarts need confirmation in the UI, have a timeout, and are logged (Diagnostics page and `logs/events.jsonl`).
- Action POSTs require JSON and the header `X-Requested-With: aio-dashboard`, which blocks plain cross-site form posts.
- Downloads are limited to the configured roots: realpath containment rejects `..`, absolute paths and escaping symlinks. There is no upload, delete or rename.
- Each port serves only its own routes. Maintenance APIs are not reachable on :8080.
- There is no authentication in v1. Add it before exposing the dashboard beyond the closed LAN.

## Not in v1 (spec §39)

AIO NAV stop/restart buttons, editable NAV destination, recording, offline map tiles,
trend charts, deep IMU/camera/LiDAR diagnostics, and ROS message browsing.
