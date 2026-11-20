# Architecture

For developers: how the dashboard is built, the rules between its parts, and how it is tested.
What each feature does and how features relate is in [FEATURES.md](FEATURES.md) (Traditional
Chinese); the main sequences are drawn in [FLOWS.md](FLOWS.md); where the project is going is in
[DIRECTION.md](DIRECTION.md).

Contents:

1. [Layers and packages](#1-layers-and-packages)
2. [The health step: state, readiness, environment](#2-the-health-step-state-readiness-environment)
3. [Collectors, actions and watchers](#3-collectors-actions-and-watchers)
4. [Decoder notes](#4-decoder-notes)
5. [Live view](#5-live-view)
6. [Optional modules](#6-optional-modules)
7. [Tests and documentation checks](#7-tests-and-documentation-checks)
8. [Not included](#8-not-included)

---

## 1. Layers and packages

One process, shared state, two HTTP servers: the user pages on `:8080` and the Maintenance view on
`:8081`. AIO NAV (`aio_nav_node`, from `../aio-nav-ros`) always sends its 0x04 packet to
`127.0.0.1:9000`; that is the dashboard's main input.

```
aio_system_dashboard/
  __main__.py          wiring: builds every part, connects the layers, starts the two servers
  config.py            config/dashboard.yaml + the active aio-nav-ros config file (re-read when it changes)
  modules.py           optional modules (installed only with --with-<name>; today: replay)
  nav/                 decoder.py (0x04), trajectory.py (last 60 s @10 Hz + older @1 Hz)
  collectors/          nav_udp (raw rate), system, services, network, ros2 (sampled), gnss_monitor
                       (GNSS lamp), gnss_diag (GNSS / RTK page), gnss_time (clock -> chrony), speed,
                       recording (is AIO NAV writing its logs), dso_watchdog, ros_guard, hotspot_relay, fake
  media/               on-demand camera image / point-cloud preview (tap.py), decoders, fake frames
  state/               health.py (the 5 Hz step), readiness.py (states, actions, reasons, by name),
                       environment.py (network / RTK / vehicle), still.py (still detection),
                       indicator.py (debounced lamps), store.py (shared state, event log)
  actions/             fixed actions only: process_control (AIO NAV / DSO), service_control (drivers),
                       autostart, driver_watchdog, diagnostics, aionav_config (config editor),
                       terminal (shell sessions), wifi (NetworkManager, with rollback; hotspot)
  data_access/         read-only browse / download inside the configured roots
  web/                 Flask apps: product.py, setup.py, maintenance.py, still.py, tools.py (editor,
                       terminal), auth.py, access.py, settings_api.py (Wi-Fi / environment on both
                       apps), server.py, i18n.py + i18n_zh.py (user-page languages)
  templates/, static/  Bootstrap + Alpine.js + Leaflet + xterm.js (vendored, offline)
  vehicle/             output to the vehicle: output.py, protocol.py, packet.py (0x05), sender.py,
                       gate.py (reference receiver)
aio_dashboard_replay/  optional module: Live / Bag replay switch and bag player
aio_sysmon/            optional system recorder (a separate service)
```

The parts are layered after what the program is for (monitoring first; see
[DIRECTION.md](DIRECTION.md)):

| Layer | Purpose | Code |
|-------|---------|------|
| 1. Monitoring (core) | See what is happening | `collectors/`, `nav/`, `state/`, the pages |
| 2. Diagnosis | Find the cause | Maintenance pages, `gnss_diag`, `media/`, the event log |
| 3. Repair and settings | Fix and adjust | `actions/` (process and driver control, config editor, Wi-Fi), Setup, Still detection |
| 4. Running unattended | Act when nobody is there | `actions/autostart.py`, the watchers ([section 3](#3-collectors-actions-and-watchers)) |
| 5. Output to the vehicle | Tell a vehicle when it may use the data | `vehicle/` |

**The rules between them:**

- A lower layer never depends on a higher one: without layers 4 and 5 the dashboard still monitors
  and diagnoses.
- **The core never imports `vehicle/`.** The health step hands its snapshot to listeners
  (`HealthEngine.snapshot_listeners`) and asks other layers for their start-up steps
  (`extra_steps`); `__main__.py` connects the vehicle layer to both. A test checks the rule
  (`tests/test_vehicle_layer.py`), so the layer can be left out or moved to another project.
- **The core never imports an optional module directly**; it asks `modules.py` ([section 6](#6-optional-modules)).
- **The web requests never carry commands.** Actions are fixed names; each target comes from a list
  built from the settings. The terminal is the one exception, behind the password.

Rates: packets are processed at the raw rate (100 Hz); the health step runs at 5 Hz; the browser
polls `/api/state` at 5 Hz and the trajectory every 2 s.

## 2. The health step: state, readiness, environment

Every 0.2 s `HealthEngine.step()` (`state/health.py`) reads the store and produces one snapshot.
The pages, `/api/readiness`, the automatic start and the 0x05 packet all read that snapshot, so
they never disagree. [FLOWS.md 2](FLOWS.md#2-one-health-tick-5-hz) draws the step.

### 2.1 The environment

`state/environment.py` decides, from the store's `network`, `gnss` and `gnss_diag` sections, where
the device is: network (wired / Wi-Fi / none), RTK (yes once corrections or an RTK position arrived
in this power-on; no without NTRIP or without a network; unknown otherwise) and vehicle (a vehicle
address in `aio_nav.yaml`). A detected change must hold 10 s. A choice made on a page wins
(`state/environment.json`). It decides whether READY needs RTK, and what the user pages say.

### 2.2 The page state

| State | Condition |
|-------|-----------|
| **STOPPED** | AIO NAV not running and nobody asked it to run (normal, not a fault) |
| **STARTING** | asked to start, or running without output yet (up to `startup_timeout_s`, 60 s); also while the automatic start waits |
| **INITIALIZING** | packets fresh but the `ready_requires` flags (default `alignment`, `heading_valid`, `fine_alignment`) not all set |
| **READY** | process up, packets fresh, alignment complete, and the required conditions held: RTK where the environment has it, plus anything in `readiness.require` |
| **DEGRADED** | was READY in this AIO NAV run, and a required condition or the alignment was lost |
| **FAULT** | AIO NAV stopped although it should run, no output after the start-up time, output stopped for `stale_fault_s` (2 s) or frozen, the UDP listener failed, AIO NAV running twice, or the automatic start gave up |

A state gets worse after about 0.5 s and recovers after about 2 s (`state/indicator.py`), so a
single missed sample does not flicker. Camera / LiDAR not connected, the sensor link down and low
disk are advisories: they never change READY.

### 2.3 Readiness

`state/readiness.py` is a pure function of the step's inputs. It gives a finer state (BOOTING,
CHECKING, NAV_STARTING, KEEP_STILL, MOVE_STRAIGHT, REFINING, READY, DEGRADED, FAULT, MAINTENANCE),
a requested action (HOLD_STILL, SAFE_STOP, …), the conditions that hold (`health`) and every reason
not to use the output (`inhibit`; never empty unless READY). **Everything is by name**; the numbers
on the wire are in `vehicle/protocol.py` ([section 2.5](#25-the-vehicle-layer)).
[FLOWS.md 3](FLOWS.md#3-readiness-states) draws the transitions.

### 2.4 The steps of the user pages

`_startup_checks` turns the snapshot into the steps under the status: Sensors, GNSS / RTK, AIO NAV,
Alignment, Recording (from `collectors/recording.py`), Storage, and the steps of other layers (the
vehicle link). Levels: healthy, busy (blue, in progress), warning, fault, idle (grey: not used here
or not started). Each step names a Maintenance page (`maint`) for its link.

### 2.5 The vehicle layer

`vehicle/output.py` is a snapshot listener. For each snapshot it adds the wire numbers
(`protocol.wire_fields`: state and action codes, health and inhibit masks, GNSS quality code) and
the packet's session, sequence and uptime, then sends the 0x05 packet (`sender.py`, `packet.py`).
It also gives the user pages their *Vehicle link* step where a vehicle is connected. The numbers
are a contract with the vehicle ([Integration Guide](../INTEGRATION.md)): append, never renumber.

## 3. Collectors, actions and watchers

- **Collectors** (`collectors/`) only read: each runs on its own thread at its own interval and
  writes one section of the store. The ROS collectors share one node in `ros2.py`; high-rate topics
  are listened to only in sampling windows (`ros.sampling`).
- **Actions** (`actions/`) change things, through `actions/registry.py`: fixed names, targets from
  the settings, a time-out, and an event for every run.
- **Watchers** act on their own. Each touches only its own part:

| Watcher | Code | Restarts |
|---------|------|----------|
| Automatic start | `actions/autostart.py` | AIO NAV + DSO (and starts the drivers first) |
| DSO watchdog | `collectors/dso_watchdog.py` | DSO only |
| Driver watchdog | `actions/driver_watchdog.py` | the sensor driver unit |
| ROS isolation guard | `collectors/ros_guard.py` | the dashboard |
| systemd watchdog | `deploy/aio-dashboard.service` | the dashboard |

When and how often is in [FLOWS.md 7](FLOWS.md#7-who-restarts-what), with the one known overlap
(the driver watchdog and the guard react to the same symptom). Add a watcher only after checking
that table.

AIO NAV and DSO run in their own sessions (`start_new_session`, `KillMode=process`), so a dashboard
crash or restart never stops them; the dashboard adopts them when it comes back.

## 4. Decoder notes

`nav/decoder.py` started from setup_env's decoder, with these fixes:

- After the wire bit reversal, wire flag bit 3 is **VUPT** (odometry update), not `imu_valid`.
- The third velocity on the wire is **Up** (`-vel_d`). It is exposed as `velocity_up`.
- The packet is 156 bytes: 5 header, 150 payload `<Qdd28fH3f`, 1 checksum.

The packet itself is described for integrators in the [Integration Guide](../INTEGRATION.md).

## 5. Live view

The Camera and LiDAR pages each have a view-only preview of the ROS topic set in
`devices.<name>.preview` (`media/tap.py`):

- **Camera** (`kind: image`): about 2 frames/s. `sensor_msgs/CompressedImage` is passed through
  unchanged; `sensor_msgs/Image` is downscaled to `max_width` and sent as JPEG (with OpenCV) or PNG.
- **LiDAR** (`kind: pointcloud`): `sensor_msgs/PointCloud2` is randomly downsampled to `max_points`
  and drawn on a canvas, top view or tilted 3D view, coloured by height or intensity.

The dashboard subscribes only while a page asks for frames, and unsubscribes 10 s after the last
request. Messages are received without deserialization; only the latest one is converted, at the
rate the browser asks for it, so the dashboard is never a permanent consumer of heavy sensor
streams. The preview needs numpy, which ships with ROS 2.

## 6. Optional modules

A **module** is an optional part that is a separate Python package next to
`aio_system_dashboard` (today only `aio_dashboard_replay/`). It provides `NAME`, `TITLE`,
`DEFAULTS` and `register(ctx, app)`, and is listed in `aio_system_dashboard/modules.py`. The core
never imports it directly: it asks `modules.py` whether the module is installed and on
(`modules.<name>.enabled`), and the module adds its own Maintenance page, routes, templates and
static files. `deploy/install.sh --with-<name>` copies it; `tools/build_release.sh --with-<name>`
compiles it into a release.

The installer's other optional parts (`sysmon`, `wifi`, `hotspot`) are system changes, not modules
([Installation Guide](../INSTALL.md#3-optional-parts)).

New optional features follow the module pattern where they can (own folder, own flag, own
`enabled` setting) and are developed on a short-lived `feature/<name>` branch merged into `main`.

## 7. Tests and documentation checks

```bash
python3 -m pip install --target .devdeps flask pyyaml psutil   # once
PYTHONPATH=.devdeps python3 -m unittest discover -s tests -t .
```

The tests need no ROS and no sensors. One test (`test_tools`, the isolated test start of the
config editor) needs `unshare -rn`; it fails in containers that do not allow it.

Some tests check the documents against the code, so the two cannot drift apart:

| Test | Checks |
|------|--------|
| `test_status_packet` | The golden 0x05 packet in the Integration Guide (and the older zh-TW operation manual) is what the code encodes |
| `test_vehicle_layer` | The published codes and bit numbers; every core name has a number; the core never imports `vehicle/` |
| `test_i18n` | Every phrase of the user pages has a translation; the zh-TW manuals use the words of `TERMINOLOGY.zh-TW.md` |
| `test_environment` | The texts the Settings page shows about the environment are translated |

A change that alters what a page shows or what a setting does updates the matching guide in the
same commit ([docs/dev/DOCS_PLAN.md](DOCS_PLAN.md) lists the guides and their readers).

## 8. Not included

Offline map tiles, trend charts (except the GNSS page's last 10 minutes), deep IMU / camera /
LiDAR diagnostics, ROS message browsing, and recording bags. The shipped settings have no LiDAR; add
a `lidar` device to use the LiDAR page. Ideas kept for later are in [IDEAS.md](IDEAS.md).
