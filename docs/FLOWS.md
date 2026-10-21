# AIO System Dashboard — Engineering Flows

Short diagrams of how the parts work together. They are Mermaid: GitHub draws them, and they are
plain text, so update them in the same commit as the code they describe. The operator view of the
same sequence (what the page and the vehicle show at each step) is in the
[Operation manual, section 4.2](OPERATION.md).

1. [System and data flow](#1-system-and-data-flow)
2. [One health tick (5 Hz)](#2-one-health-tick-5-hz)
3. [Readiness states](#3-readiness-states)
4. [Automatic start (every 1 s)](#4-automatic-start-every-1-s)
5. [Wi-Fi change](#5-wi-fi-change)
6. [Vehicle side](#6-vehicle-side)

## 1. System and data flow

What runs on the Jetson, and what goes over which port. Solid lines are data, the dotted line is
control.

```mermaid
flowchart TB
  HW["IMU / GNSS (OpenRTK330), camera"] --> DRV["Sensor drivers<br/>(user unit)"]
  DRV -- "ROS 2 topics" --> NAV["AIO NAV<br/>aio_nav_node"]
  DRV -- "camera" --> DSO["DSO"]
  DSO -- "odometry (VUPT)" --> NAV
  NAV -- "0x04, 100 Hz<br/>127.0.0.1:9000" --> COL["Dashboard: NAV UDP collector"]
  COL --> HE["Dashboard: health engine, 5 Hz<br/>+ system, services, network, ROS 2, GNSS, DSO watchdog"]
  HE --> SNAP[("Readiness snapshot")]
  SNAP --> UI["User UI :8080<br/>Maintenance UI :8081"]
  SNAP --> TX["0x05 sender<br/>2 Hz + on change"]
  SNAP --> AUTO["Automatic start<br/>→ process control"]
  NAV -- "0x04, output_udp" --> VEH[("Vehicle")]
  TX -- "0x05, same address and port" --> VEH
  AUTO -. "starts / stops" .-> NAV
```

- One snapshot (`state/readiness.py`) feeds the pages, `/api/readiness` and the 0x05 packet, so
  they never disagree.
- The dashboard is the only listener on 127.0.0.1:9000; `output_udp` in `aio_nav.yaml` is the
  vehicle. 0x05 goes to that same address and port (`status_udp.destination: auto`).
- Process control starts and stops AIO NAV and DSO together (`nav.control_group`); the automatic
  start also starts the sensor drivers first (`autostart.start_services`).
- AIO NAV keeps running if the dashboard stops; the dashboard adopts it when it comes back.

## 2. One health tick (5 Hz)

`HealthEngine.step()` in `state/health.py`.

```mermaid
flowchart TD
  A["Read the store:<br/>services, NAV packets, GNSS, ROS 2, network, system"] --> B["Raw product state<br/>raw_product_state()"]
  B --> C["Debounce<br/>raise 0.5 s, clear 2 s"]
  C --> D["Readiness conditions with hysteresis<br/>stable 2 s, loss 1 s (GNSS: 3 s / 3 s)"]
  D --> E["readiness.evaluate()<br/>pure function, no I/O"]
  E --> F["Snapshot: state, action, health and<br/>inhibit masks, reasons, session, sequence"]
  F --> G["Page view: status text,<br/>start-up steps, lamps"]
  F --> H[("store: product, readiness, maint")]
  F --> I{"0x05 due (0.5 s)<br/>or state / action / inhibit changed?"}
  I -- yes --> J["Send 42-byte 0x05"]
  I -- no --> K["Wait for the next tick"]
```

- Not READY always has at least one inhibit bit; READY always has none (tested exhaustively).
- The READY latch is per AIO NAV process: after a crash it says SAFE_STOP, not "starting".

## 3. Readiness states

The main transitions of `readiness.evaluate()`. Codes are the 0x05 `state` values.

```mermaid
stateDiagram-v2
  [*] --> BOOTING
  BOOTING --> CHECKING: automatic start waits
  BOOTING --> MAINTENANCE: automatic start off, not running
  CHECKING --> NAV_STARTING: AIO NAV started
  MAINTENANCE --> NAV_STARTING: Start / Resume
  NAV_STARTING --> KEEP_STILL: first 0x04
  KEEP_STILL --> MOVE_STRAIGHT: alignment
  MOVE_STRAIGHT --> REFINING: heading_valid
  REFINING --> READY: fine_alignment and required conditions
  READY --> DEGRADED: condition or alignment lost
  DEGRADED --> READY: condition back
  READY --> FAULT: AIO NAV stopped by itself
  CHECKING --> FAULT: prerequisites missing 120 s
  NAV_STARTING --> FAULT: no output in 60 s
  FAULT --> NAV_STARTING: Start or automatic retry (after READY: vehicle still)
  READY --> MAINTENANCE: operator Stop
```

| State | Requested action | Notes |
|-------|------------------|-------|
| BOOTING, CHECKING, NAV_STARTING | HOLD_STILL | |
| KEEP_STILL / MOVE_STRAIGHT / REFINING | HOLD_STILL / DRIVE_STRAIGHT / KEEP_DRIVING | from the first missing alignment flag |
| READY | NONE | inhibit_flags = 0 |
| DEGRADED | SAFE_STOP | only after READY in this AIO NAV run |
| FAULT | SERVICE_REQUIRED, or SAFE_STOP after READY | also: AIO NAV running twice, or `aio-nav.service` enabled with automatic start (OWNERSHIP_CONFLICT) |
| MAINTENANCE | MAINTENANCE_HOLD | operator Stop (until Resume or the next power-on) or nobody asked it to run |

## 4. Automatic start (every 1 s)

`StartupCoordinator.tick()` in `actions/autostart.py`. It judges success by AIO NAV only; DSO
failing never restarts a working AIO NAV.

```mermaid
flowchart TD
  T(["tick"]) --> E{"enabled?"}
  E -- no --> X0["disabled"]
  E -- yes --> RD["READY now: remember it for this boot,<br/>attempts = 0"]
  RD --> H{"operator hold?"}
  H -- yes --> X1["held<br/>until Start / Resume or the next power-on"]
  H -- no --> U{"AIO NAV running?"}
  U -- yes --> F{"no output, or<br/>output stopped?"}
  F -- no --> X3["running"]
  F -- yes --> I{"an input missing?<br/>(no IMU, no valid time)"}
  I -- yes --> X7["waiting_inputs<br/>left running: may resume"]
  I -- no --> G{"stale_grace_s over?<br/>(10 s; 0 s when no output at all)"}
  G -- no --> X8["recovering"]
  G -- yes --> A1{"max_attempts reached?<br/>(0 = never)"}
  A1 -- yes --> X4["gave_up<br/>left running for the logs"]
  A1 -- no --> S1["stop it, retry later<br/>5, 15, 30, 60, 60 … s"]
  U -- no --> A2{"max_attempts reached?"}
  A2 -- yes --> X4
  A2 -- no --> W{"retry wait over?"}
  W -- no --> X5["retry_wait"]
  W -- yes --> SV["start_services: drivers not running,<br/>not stopped by an operator,<br/>manager answers (at most every 30 s)"]
  SV --> P{"prerequisites stable 2 s,<br/>clock matches GNSS time (≤ 120 s),<br/>vehicle still 5 s?"}
  P -- no --> X6["waiting<br/>prereq_timeout after 120 s<br/>(missing inputs only), keeps waiting"]
  P -- yes --> L["attempts + 1<br/>start AIO NAV and DSO"]
```

- The hold, the attempt count and "READY reached" are stored with
  `/proc/sys/kernel/random/boot_id` (`state/autostart.json`, or `/dev/shm` if `state/` cannot be
  written): a dashboard restart keeps them, the next power-on starts afresh.
- After READY the same path restarts AIO NAV; meanwhile readiness says FAULT / SAFE_STOP, and the
  start waits for the vehicle to be still.
- An operator Stop sets the hold; Start / Resume clears it and resets the attempts. A busy process
  slot (the DSO watchdog starting DSO) is not an attempt: it tries again in 2 s.

## 5. Wi-Fi change

`register_settings()` in `web/settings_api.py` and `WifiManager.connect()` in `actions/wifi.py`.
Same code for the user page (no password) and the Maintenance page (password).

```mermaid
flowchart TD
  A["POST /api/wifi/connect<br/>(or /api/maint/wifi/connect)"] --> B{"browser reaches the device<br/>over Ethernet or loopback?"}
  B -- no --> R1["403: connect with a cable"]
  B -- yes --> C{"navigation in use, or vehicle /<br/>internet route over Wi-Fi?"}
  C -- "yes, not confirmed" --> R2["409: needs_confirm"]
  C -- "no, or confirmed" --> D["remember the active Wi-Fi profile<br/>and the existing profiles"]
  D --> E["nmcli device wifi connect<br/>LC_ALL=C, output scrubbed of the password"]
  E --> F{"joined?"}
  F -- yes --> G{"IPv4 within 30 s?"}
  G -- yes --> OK["success"]
  F -- no --> RB["rollback: delete only the new Wi-Fi profiles,<br/>bring the previous profile up"]
  G -- no --> RB
  RB --> FAIL["failure with a code:<br/>wrong_password, not_found, no_ip, denied, timeout"]
```

- Runs without root after a one-time `sudo deploy/install.sh --with-wifi` (polkit rule for the
  service user only).
- One change at a time (a lock in the one `WifiManager` both apps share).

## 6. Vehicle side

The reference receiver, `nav/vehicle_gate.py` (`tools/vehicle_gate.py` runs it on live UDP).

```mermaid
flowchart TD
  A["datagram on the output_udp port"] --> B{"55 AA and byte 2?"}
  B -- "0x05, 42 bytes" --> C{"checksum and version 1?<br/>same session_id?"}
  C -- no --> BL0["clear the authorisation"]
  C -- yes --> S["keep the status; READY with inhibit 0<br/>starts the 2 s timer"]
  B -- "0x04, 156 bytes" --> N["keep the newest 0x04"]
  B -- other --> IG["ignore"]
  S --> Q
  N --> Q{"for every use:<br/>status at most 3 s old?<br/>READY, inhibit 0 for 2 s?<br/>0x04 at most 0.5 s old, flags set,<br/>time forward, values finite?"}
  Q -- all yes --> USE["use the navigation data"]
  Q -- any no --> BLOCK["do not use"]
```
