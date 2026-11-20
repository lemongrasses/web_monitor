# Architecture

For developers: how the dashboard is built, as diagrams first. Each section starts with a picture,
then the few rules it shows, then **Details** links to the documents and code to read when
something is unclear. What each feature does and how features relate is in
[FEATURES.md](FEATURES.md) (Traditional Chinese); the run-time sequences are in [FLOWS.md](FLOWS.md).

The diagrams are [Mermaid](https://mermaid.js.org/): GitHub draws them, and they are plain text, so
they change in the same commit as the code they describe.

Contents:

1. [The device at a glance](#1-the-device-at-a-glance)
2. [Layers](#2-layers)
3. [Packages and who imports whom](#3-packages-and-who-imports-whom)
4. [Inside the dashboard process](#4-inside-the-dashboard-process)
5. [One health step](#5-one-health-step)
6. [States](#6-states)
7. [Who restarts what](#7-who-restarts-what)
8. [Where a setting comes from](#8-where-a-setting-comes-from)
9. [A request from a page](#9-a-request-from-a-page)
10. [Optional modules](#10-optional-modules)
11. [Tests that check the documents](#11-tests-that-check-the-documents)
12. [Notes](#12-notes)

---

## 1. The device at a glance

What runs on the device, what it talks to, and over what.

```mermaid
flowchart LR
  SENS["IMU / GNSS receiver<br/>camera"] --> DRV
  NTRIP[("RTK corrections<br/>NTRIP, via Internet")] -.-> DRV
  subgraph DEV["AIO device (Jetson)"]
    DRV["Sensor drivers<br/>systemd user unit"]
    NAV["AIO NAV<br/>navigation filter"]
    DSO["DSO<br/>visual odometry"]
    DASH["Dashboard<br/>aio-dashboard.service"]
    CHR["chrony<br/>system clock"]
    REC["aio-sysmon<br/>(optional)"]
  end
  DRV -- "ROS 2" --> NAV
  DRV -- "camera" --> DSO
  DSO -- "odometry" --> NAV
  NAV -- "0x04, 127.0.0.1:9000" --> DASH
  DRV -- "ROS 2 (watched)" --> DASH
  DASH -- "IMU time stamps" --> CHR
  DASH -. "start / stop" .-> NAV
  DASH -. "start / stop" .-> DSO
  DASH -. "start / restart" .-> DRV
  NAV -- "0x04, output_udp" --> VEH[("Vehicle or<br/>your application")]
  DASH -- "0x05, same port" --> VEH
  U["Browser :8080<br/>user pages"] --> DASH
  E["Browser :8081<br/>Maintenance, password"] --> DASH
  DASH -. "copies of 0x04 / 0x05" .-> PH["Phones on<br/>the hotspot"]
```

- AIO NAV computes the navigation; the dashboard watches it and everything it depends on. AIO NAV
  sends its output to the vehicle by itself; the dashboard adds the 0x05 readiness packet.
- AIO NAV, DSO and the drivers run in their own processes: a dashboard restart never stops them.
- Dotted arrows are control; solid arrows are data.

**Details:** [Integration Guide](../INTEGRATION.md) (0x04, 0x05), [Installation Guide 5](../INSTALL.md#5-what-the-installer-changes-on-the-system)
(services and system changes), [FLOWS.md 1](FLOWS.md#1-system-and-data-flow).

## 2. Layers

The parts are layered after what the program is for: monitoring first.

```mermaid
flowchart TB
  L5["5. Output to the vehicle<br/>vehicle/ · 0x05, protocol numbers, reference receiver"]
  L4["4. Running unattended<br/>autostart · DSO watchdog · driver watchdog · ROS guard"]
  L3["3. Repair and settings<br/>process / driver control · config editor · Wi-Fi · Setup · still detection"]
  L2["2. Diagnosis<br/>Maintenance pages · GNSS / RTK · live view · event log"]
  L1["1. Monitoring (core)<br/>collectors · health step · readiness · environment · user pages"]
  L5 --> L4 --> L3 --> L2 --> L1
```

- An arrow means "uses". **A layer only uses the layers below it**: without layers 4 and 5 the
  dashboard still monitors and diagnoses.
- Layer 5 may move to another project one day; that is why it is a package of its own
  ([section 3](#3-packages-and-who-imports-whom)).

**Details:** [DIRECTION.md 2](DIRECTION.md) (why these layers, Traditional Chinese).

## 3. Packages and who imports whom

The import graph of `aio_system_dashboard/`, as it is in the code.

```mermaid
flowchart TB
  subgraph W["Wiring"]
    MAIN["#95;#95;main#95;#95;.py"]
  end
  subgraph APPS["Pages"]
    WEB["web/<br/>Flask apps"]
    REPLAY["aio_dashboard_replay<br/>optional module"]
  end
  subgraph L5["Layer 5"]
    VEH["vehicle/<br/>0x05"]
  end
  subgraph CHG["Changes"]
    ACT["actions/"]
  end
  subgraph CORE["Health step"]
    ST["state/"]
  end
  subgraph READ["Readers"]
    COL["collectors/"]
    MED["media/"]
    DATA["data_access/"]
  end
  subgraph BASE["Base"]
    NAVP["nav/<br/>0x04 decoder"]
    MODS["modules.py"]
  end
  MAIN --> WEB
  MAIN --> VEH
  MAIN --> MODS
  WEB --> ACT
  WEB --> ST
  WEB --> MED
  WEB --> DATA
  REPLAY --> ACT
  VEH --> ST
  VEH --> NAVP
  ACT <--> ST
  ACT --> COL
  MED --> COL
  COL --> NAVP
  MODS -. "imports only if installed" .-> REPLAY
  classDef layer5 fill:#fde8d8,stroke:#c05621
  class VEH layer5
```

- Arrows point from the importer to what it imports. Not drawn: almost every package imports
  `config`, and `__main__.py` imports everything (it builds the parts).
- **Only `__main__.py` imports `vehicle/`.** The health step hands its snapshot to listeners; the
  wiring connects the vehicle layer to them. `tests/test_vehicle_layer.py` fails if any other module
  imports `vehicle/`.
- **The core never imports an optional module**; it asks `modules.py`
  ([section 10](#10-optional-modules)).
- `state` and `actions` use each other: the health step asks `process_control` whether AIO NAV is
  wanted, and the automatic start uses the readiness conditions. Known; keep it to those two uses.

**Details:** the package list in [section 12](#12-notes).

## 4. Inside the dashboard process

One process. Readers write the shared store; the health step turns it into one snapshot; everyone
else reads.

```mermaid
flowchart LR
  subgraph IN["Collectors (one thread each)"]
    C1["nav_udp<br/>100 Hz"]
    C2["system · services<br/>network · recording"]
    C3["ros2<br/>GNSS · DSO · speed · IMU"]
  end
  STORE[("Store<br/>one section per writer")]
  C1 & C2 & C3 --> STORE
  STORE --> HS["Health step<br/>every 0.2 s"]
  HS -- "snapshot" --> STORE
  HS -- "snapshot" --> VO["vehicle/ listener<br/>0x05 sender"]
  STORE --> PAGES["Web apps<br/>:8080 · :8081"]
  STORE --> AUTO["Automatic start<br/>every 1 s"]
  STORE --> WD["Watchers<br/>driver · DSO · ROS guard"]
  PAGES -- "fixed actions" --> REG["Action registry"]
  AUTO & WD --> REG
  REG -- "start / stop / restart" --> PROC["AIO NAV · DSO · drivers"]
```

- **One writer per store section.** Readers get copies, so no reader can change another part's data.
- **Every change goes through the action registry**: fixed action names, targets from the settings,
  a time-out, an event in the log.
- The browser asks for `/api/state` five times a second and for the trajectory every 2 s.

**Details:** `state/store.py`, `actions/registry.py`, [FLOWS.md 2 and 4](FLOWS.md).

## 5. One health step

What `HealthEngine.step()` (`state/health.py`) does five times a second.

```mermaid
flowchart TD
  A["Read the store<br/>services · NAV packets · GNSS · ROS · network · system"] --> B
  B["Environment<br/>network · RTK · vehicle<br/>state/environment.py"] --> C
  C["Page state<br/>STOPPED … READY … FAULT<br/>debounced: worse 0.5 s, better 2 s"] --> D
  D["Readiness<br/>10 phases, action, health and inhibit names<br/>state/readiness.py, pure"] --> E
  E["Listeners<br/>vehicle/: add the numbers, send 0x05"] --> F
  F["User-page view<br/>headline · steps · lamps · advisories"] --> G
  G[("Store<br/>product · readiness · environment · maint")]
```

- **One snapshot for everything**: the pages, `/api/readiness`, the automatic start and the 0x05
  packet read the same one, so they never disagree.
- Readiness works **by name**; the numbers on the wire are added by the vehicle layer.
- A listener that fails is logged and skipped; it cannot stop the step.

**Details:** [FLOWS.md 2](FLOWS.md#2-one-health-tick-5-hz), [User Guide 3](../USER_GUIDE.md#3-reading-the-status)
(what the pages show).

## 6. States

The six states the pages show. Readiness splits them into ten phases for the vehicle
([FLOWS.md 3](FLOWS.md#3-readiness-states)).

```mermaid
stateDiagram-v2
  [*] --> STOPPED
  STOPPED --> STARTING: Start, or automatic start
  STARTING --> INITIALIZING: first output
  INITIALIZING --> READY: aligned and required conditions held
  READY --> DEGRADED: a required condition or the alignment lost
  DEGRADED --> READY: back and stable
  STARTING --> FAULT: no output in 60 s
  READY --> FAULT: AIO NAV stopped by itself
  INITIALIZING --> FAULT: output stopped or frozen
  FAULT --> STARTING: restarted
  READY --> STOPPED: operator Stop
  FAULT --> STOPPED: operator Stop
```

| State | Means |
|-------|-------|
| STOPPED | Not running, nobody asked it to: normal |
| STARTING | Being started, or the automatic start waits |
| INITIALIZING | Running and aligning |
| READY | Aligned, output fresh, required conditions held (RTK where the environment has it) |
| DEGRADED | Was READY in this run; something required was lost |
| FAULT | AIO NAV has a problem (the reason is shown) |

**Details:** [User Guide 3.1](../USER_GUIDE.md#31-the-headline), [Integration Guide 6.3](../INTEGRATION.md#63-states)
(the phases on the wire).

## 7. Who restarts what

Five watchers act on their own. Each touches only its own target.

```mermaid
flowchart LR
  AS["Automatic start<br/>AIO NAV should run but does not,<br/>or sends nothing"] --> NAVT["AIO NAV + DSO"]
  AS -. "first, if not running" .-> DRVT["Sensor drivers"]
  DW["DSO watchdog<br/>NaN · gone · too much memory"] --> DSOT["DSO only"]
  DRW["Driver watchdog<br/>IMU silent 45 s"] --> DRVT
  RG["ROS isolation guard<br/>no other ROS node visible 30 s"] --> DASHT["The dashboard"]
  SD["systemd watchdog<br/>dashboard silent 60 s"] --> DASHT
  OP(["Operator Stop"]) -. "holds" .-> AS
  OP -. "holds" .-> DRW
```

- Every watcher waits longer after each failure and respects an operator Stop.
- **Known overlap:** when the dashboard's own ROS connection is stuck, the IMU also looks silent, so
  the driver watchdog and the ROS guard can both act on the same symptom.
- Check this picture before adding a watcher.

**Details:** [FLOWS.md 7](FLOWS.md#7-who-restarts-what) (timings), [Maintenance Guide 3](../MAINTENANCE.md#3-how-the-device-runs-on-its-own).

## 8. Where a setting comes from

Some answers can come from three places. The first that has one wins. RTK, as the example:

```mermaid
flowchart TD
  Q{"Does READY need RTK?"} --> P{"Set by hand on a page?<br/>state/environment.json"}
  P -- "yes" --> PV["Use that choice"]
  P -- "no" --> C{"readiness.require_rtk<br/>true or false in dashboard.yaml?"}
  C -- "yes" --> CV["Use the config"]
  C -- "no, auto" --> D{"Detected?"}
  D -- "RTK here" --> Y["Required"]
  D -- "no RTK here" --> N["Not required"]
  D -- "not known yet" --> Y
```

- The same pattern for the other page choices: the page's choice is kept in `state/` and wins over
  `dashboard.yaml`; some last until the next power-on (Live / Bag replay, operator Stop).
- *Not known* counts as required: the careful choice for a vehicle.

**Details:** [FLOWS.md 8](FLOWS.md#8-where-a-setting-comes-from) (every such setting),
[Configuration Reference](../CONFIG.md), `state/environment.py`.

## 9. A request from a page

What happens when someone presses a button, for example **Stop** on the user pages.

```mermaid
sequenceDiagram
  participant B as Browser
  participant S as Web server (port)
  participant A as Flask app
  participant G as Guard
  participant R as Action registry
  participant P as AIO NAV
  B->>S: POST /api/nav/control {"action": "stop"}
  S->>S: allowed client? (access.allowed_clients)
  S->>A: only this port's routes
  A->>A: JSON and X-Requested-With header?
  A->>G: Maintenance only: signed in?
  A->>R: fixed action "stop_process", target from the settings
  R->>P: SIGTERM, then SIGKILL after a grace period
  R-->>A: result, with a time-out
  R->>R: event in the log
  A-->>B: {"success": true, "summary": "AIO NAV stopped"}
```

- **No commands from the web**: a request names a fixed action; its target comes from the settings.
  The Maintenance terminal is the one exception, behind the password.
- The user pages need no password; what they can change is listed in
  [Installation Guide 7](../INSTALL.md#7-security).

**Details:** `web/server.py`, `web/access.py`, `web/auth.py`, `actions/registry.py`.

## 10. Optional modules

How a module plugs in without the core knowing it.

```mermaid
sequenceDiagram
  participant I as install.sh --with-replay
  participant M as __main__
  participant MO as modules.py
  participant R as aio_dashboard_replay
  participant W as Maintenance app
  I->>R: copy the package next to the dashboard
  M->>MO: load(settings)
  MO->>MO: installed? (find_spec) and modules.replay.enabled?
  MO->>R: import
  M->>R: register(ctx, app)
  R->>W: its page, routes, templates, static files
```

- A module provides `NAME`, `TITLE`, `DEFAULTS` and `register(ctx, app)`, and is listed in
  `modules.py`. A module that fails to load is logged and skipped.
- The installer's other optional parts (`sysmon`, `wifi`, `hotspot`) are system changes, not
  modules.

**Details:** `aio_system_dashboard/modules.py`, [Installation Guide 3](../INSTALL.md#3-optional-parts).

## 11. Tests that check the documents

Some tests read the documents, so the documents cannot drift away from the code.

```mermaid
flowchart LR
  T1["test_status_packet"] --> D1["Integration Guide<br/>golden 0x05 packet"]
  T2["test_vehicle_layer"] --> D2["Published codes and bits<br/>core never imports vehicle/"]
  T3["test_i18n"] --> D3["zh-TW manuals and pages<br/>TERMINOLOGY.zh-TW.md words"]
  T4["test_environment"] --> D4["Environment texts translated"]
  CODE[("The code")] --- T1 & T2 & T3 & T4
```

```bash
python3 -m pip install --target .devdeps flask pyyaml psutil   # once
PYTHONPATH=.devdeps python3 -m unittest discover -s tests -t .
```

- The tests need no ROS and no sensors. One test (the config editor's isolated test start) needs
  `unshare -rn` and fails in containers that do not allow it.
- A change that alters what a page shows or what a setting does updates the matching guide in the
  same commit ([DOCS_PLAN.md](DOCS_PLAN.md) lists the guides and their readers).

## 12. Notes

**Packages.**

| Package | Holds |
|---------|-------|
| `__main__.py` | The wiring: builds every part, connects the layers, starts the two servers |
| `config.py` | `dashboard.yaml` and the active aio-nav-ros settings file (re-read when it changes) |
| `nav/` | `decoder.py` (0x04), `trajectory.py` (last 60 s at 10 Hz, older at 1 Hz) |
| `collectors/` | `nav_udp`, `system`, `services`, `network`, `recording`, `ros2` (sampled), `gnss_monitor`, `gnss_diag`, `gnss_time`, `speed`, `dso_watchdog`, `ros_guard`, `hotspot_relay`, `fake` |
| `state/` | `health.py` (the step), `readiness.py`, `environment.py`, `still.py`, `indicator.py`, `store.py` |
| `actions/` | `registry`, `process_control`, `service_control`, `autostart`, `driver_watchdog`, `diagnostics`, `aionav_config`, `terminal`, `wifi` |
| `media/` | The live view: `tap.py`, image and point-cloud decoders, fake frames |
| `data_access/` | Read-only browse and download inside the data folders |
| `web/` | The Flask apps and their helpers; `i18n.py` + `i18n_zh.py` for the user-page languages |
| `vehicle/` | `output.py` (the listener), `protocol.py` (numbers), `packet.py`, `sender.py`, `gate.py` |
| `templates/`, `static/` | Bootstrap, Alpine.js, Leaflet, xterm.js, all vendored (no Internet needed) |

**Decoder fixes compared with setup_env** (`nav/decoder.py`): after the wire bit reversal, wire flag
bit 3 is **VUPT**, not `imu_valid`; the third velocity on the wire is **Up** (`-vel_d`), exposed as
`velocity_up`; the packet is 156 bytes (5 header, 150 payload `<Qdd28fH3f`, 1 checksum).

**Live view** (`media/tap.py`): the dashboard subscribes to a camera or LiDAR topic only while a
page asks for frames, and unsubscribes 10 s after the last request. Messages are received without
deserialization; only the latest one is converted, at the rate the browser asks for. Camera images
are passed through (CompressedImage) or downscaled (Image); point clouds are downsampled to
`max_points`. Needs numpy, which ships with ROS 2.

**Not included:** offline map tiles, trend charts (except the GNSS page's last 10 minutes), deep
IMU / camera / LiDAR diagnostics, ROS message browsing, recording bags. Ideas kept for later:
[IDEAS.md](IDEAS.md).
