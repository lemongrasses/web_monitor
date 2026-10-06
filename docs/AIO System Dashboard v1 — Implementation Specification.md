# AIO System Dashboard v1
## Product / Maintenance Web Interface Specification

## 1. 專案定位

建立一套新的、獨立的 **AIO System Dashboard**，部署於 Jetson 上。

這不是 `setup_env` 的延伸版，也不是 fork 後繼續修改。

新環境正式部署時 **不包含 setup_env**。

但開發時應盡量從 `setup_env` 重用已驗證且合適的資源，例如：

- NAV binary packet decoder
- Python offline wheels
- Bootstrap
- Alpine.js
- Leaflet
- 現有 Flask/API 實作方式中可重用的部分
- navigation status / trajectory 頁面的既有邏輯或概念

應重新設計整體架構與 UI，不沿用 `setup_env` 原本的產品結構。

---

# 2. 產品目的

這台設備的主要產品輸出是：

**AIO NAV navigation solution over UDP**

一般使用者最重要的需求不是查看 ROS 或底層 sensor debugging 資訊，而是：

1. AIO NAV 現在是否可用。
2. UDP navigation output 是否正在正常輸出。
3. 目前 navigation solution 是什麼。
4. 如果 UDP 沒資料，大致是哪一層出問題。
5. 可以透過網路下載設備上的資料。

ROS 2 目前主要屬於：

**internal development / engineering interface**

而不是主要對外產品介面。

---

# 3. Target Platform

正式目標：

- NVIDIA Jetson
- aarch64
- Ubuntu 22.04
- ROS 2 Humble
- Python 3.10
- 封閉或半封閉 Ethernet Sensor LAN
- 固定 IP 為主要網路配置方式

典型網路：

```text
Laptop
    │
    │ Ethernet
    ▼
 Switch
    │
    ▼
 Jetson
```

Laptop 與 Jetson 位於同一 subnet 後，可直接透過瀏覽器連入。

---

# 4. Web Service 分流

使用兩個獨立 Web port。

## Product UI

```text
http://<jetson-ip>:8080
```

一般使用者使用。

不需要登入。

主要功能：

- Overview
- Navigation
- Data Download

---

## Maintenance UI

```text
http://<jetson-ip>:8081
```

工程、維護與內部開發使用。

v1 暫時不做帳號密碼。

因設備原則上位於封閉 LAN，不以 Internet-facing service 的威脅模型設計。

但仍需做基本保護：

- 不提供任意 shell command
- 操作功能必須採明確白名單
- restart 等操作需 confirmation
- action 必須記錄時間與執行結果
- Data Download 限定指定目錄
- 不允許任意 filesystem traversal
- Maintenance actions 不可接受任意 command string

未來如果部署環境改變，再增加 authentication。

---

# 5. Runtime Architecture

核心元件應與 Dashboard 解耦。

建議由 `systemd` 管理主要服務。

概念：

```text
Jetson Boot
│
├── aio-nav.service
├── camera.service
├── ouster.service
├── other ROS services
└── aio-dashboard.service
```

核心 navigation / sensor services：

- 開機自動啟動
- Dashboard crash 不應造成 sensor/navigation service 停止
- service 可依 systemd policy 自動 restart

Dashboard 主要負責：

- Monitor
- Aggregate health state
- User visualization
- Limited whitelisted actions

Dashboard 不應成為所有 sensor process 的 parent process。

---

# 6. 建議 Software Architecture

建議 backend 保持模組化。

```text
aio_system_dashboard/
│
├── app/
│   ├── product/
│   ├── maintenance/
│   ├── api/
│   └── state/
│
├── collectors/
│   ├── nav_udp.py
│   ├── ros2.py
│   ├── system.py
│   ├── network.py
│   ├── camera.py
│   └── ouster.py
│
├── actions/
│   ├── registry.py
│   ├── diagnostics.py
│   └── service_control.py
│
├── nav/
│   ├── decoder.py
│   └── trajectory.py
│
├── data_access/
│
├── templates/
├── static/
├── config/
└── tests/
```

具體名稱可以調整，但應保持以下 separation：

**Collectors**
只讀取系統狀態。

**Actions**
負責會改變系統狀態的操作。

**State**
統一維護最新 snapshot。

**API**
提供 Web frontend 所需的穩定介面。

不要把 UDP listener、ROS monitoring、systemctl 與 Web route 全部塞進單一 `app.py`。

---

# 7. AIO NAV Data Path

`aio_nav_node` 目前會送出 `0x04 NAV binary packet`。

本機：

```text
aio_nav_node
     │
     │ UDP
     ▼
127.0.0.1:9000
     │
     ▼
Dashboard NAV Collector
```

新環境沒有 `setup_env`，因此不存在舊 listener 的 port conflict。

`127.0.0.1:9000` 可以由新的 Dashboard NAV collector 使用。

---

# 8. NAV Decoder

可從 `setup_env` 取得既有：

```text
decoder.py:
parse_nav_solution_packet()
```

但搬入新專案後必須修正兩個已知 mismatch。

## Flag 修正

Wire flag bit 3：

```text
Incorrect:
imu_valid

Correct:
VUPT
```

即：

```text
bit 3 = VUPT / odometry update
```

---

## Velocity 修正

AIO NAV wire format 傳送的是：

```text
velocity Up
```

而不是：

```text
velocity Down
```

因此新程式內部欄位應命名：

```text
velocity_up
```

不要再沿用：

```text
velocity_down
```

---

# 9. External NAV Output

正式對外產品介面以 UDP 為主。

例如：

```text
AIO NAV
   │
   ├── UDP binary → User application
   │
   └── ROS 2      → Internal development
```

Dashboard 只顯示目前 configured output：

```text
Destination   192.168.50.91:9000
Protocol      UDP / NAV
Output Rate   ...
Status        Streaming / Lost
```

v1：

**只顯示，不提供修改 destination IP / port。**

設定仍由系統 configuration 管理。

---

# 10. UDP Status 的語意

UDP 沒有 ACK。

因此 Dashboard 可以確認：

- aio_nav_node 是否存在
- NAV solution 是否持續產生
- sender 是否持續送出
- destination configuration
- local NIC / route 是否存在
- output rate
- last packet timestamp

但不可宣稱：

> Remote application 已經成功收到資料。

UI wording 應區分：

```text
UDP Output: Streaming
```

與：

```text
Receiver Connected
```

v1 不實作 receiver heartbeat / ACK。

---

# 11. Product Health Model

不要把所有 sensor warning 都直接變成 product fault。

核心產品狀態先保持簡單：

```text
READY
INITIALIZING
FAULT
```

另外獨立提供：

```text
Advisories / Warnings
```

---

## READY

條件概念：

- AIO NAV process/service 正常
- NAV packets 持續更新
- core navigation solution 可使用
- UDP output 正常產生

---

## INITIALIZING

例如：

- AIO NAV 已啟動
- packets 正常
- 但尚未完成必要 alignment / initialization

---

## FAULT

例如：

- AIO NAV service/process down
- NAV output 停止更新
- 核心 inertial/navigation pipeline 無法工作
- UDP navigation output 已停止

---

# 12. GNSS Health Semantics

GNSS loss **不是產品故障**。

系統以 IMU / inertial navigation 為核心，GNSS unavailable 本來就是預期可能發生的 operating condition。

因此：

```text
Navigation       READY
GNSS             Warning / Unavailable
```

是合法且正常的狀態。

例如：

```text
● READY

GNSS
⚠ Signal unavailable
Inertial navigation remains active.
```

不要因 GNSS 暫時失效就把整機顯示為：

```text
FAULT
```

或強制：

```text
DEGRADED
```

GNSS 狀況屬 advisory。

---

# 13. IMU Health — v1

v1 不實作深度 IMU diagnostics。

基礎判斷：

```text
AIO NAV running
+
NAV packet fresh
=
core inertial/navigation pipeline healthy
```

未來 Maintenance / Research 版本再加入：

- IMU raw rate
- timestamp
- validity
- sensor-specific fault
- saturation
- bias / integrity diagnostics

v1 不需要。

---

# 14. Camera / LiDAR Product Semantics

Camera 與 LiDAR：

**不決定 AIO NAV 是否 READY。**

Product UI 只做簡單 connected indication。

例如：

```text
Camera    Connected
LiDAR     Connected
```

只要裝置在基本網路層級可 reach / 存在即可。

不要在 Product UI 顯示：

- FPS deviation
- ROS topic stale
- driver warning
- dropped frames
- point cloud rate
- packet diagnostics

這些放在 Maintenance。

---

# 15. Camera / LiDAR Data Source Strategy

Dashboard **不要成為第二個 raw sensor stream consumer**。

正確架構：

```text
Camera
   │
   ▼
ROS Camera Driver
   │
   ▼
ROS Topics
   │
   ▼
Dashboard
```

以及：

```text
Ouster
   │
   ▼
ROS Ouster Driver
   │
   ▼
ROS Topics
   │
   ▼
Dashboard
```

Dashboard 可以另外直接做：

- ping / reachability
- lightweight device API
- connection status

但不要和 ROS driver 同時搶 raw payload stream。

原因包括：

- GigE stream ownership
- UDP socket conflict
- duplicated network bandwidth
- control ownership conflict
- possible packet distribution instead of duplication

因此 sensor monitoring 分兩層：

```text
Device reachable?
+
ROS data flowing?
```

---

# 16. ROS 2 Monitoring — Level 2

v1 做到 Level 2。

需監控：

## Nodes

- node 是否存在

## Topics

- topic 是否存在
- approximate publish rate
- freshness / last update age
- publisher count
- subscriber count

例如：

```text
/topic/nav           100 Hz    8 ms
/camera/image_raw     15 Hz   24 ms
/ouster/points        10 Hz   41 ms
/ouster/imu          100 Hz    7 ms
```

不做：

- full ROS graph
- message preview
- service explorer
- action explorer
- parameter editor
- deep QoS inspection
- PointCloud2/image full deserialization for monitoring

Dashboard 只需要知道：

> 資料是否正在正常流動。

---

# 17. Status Indicator Design

此部分由 implementation agent 依成熟 monitoring UI practice 實作。

核心原則：

- 不因單一 packet miss 閃爍
- 不直接把 packet callback 綁到燈號
- 使用 freshness + timeout
- 使用 debounce / hysteresis
- recovery 亦需短時間穩定後才轉回正常
- 不只依靠顏色
- 顏色必須搭配文字

推薦基本 state：

```text
Healthy
Warning / Stale
Fault / Offline
Unknown
```

視覺：

```text
● Normal
● Warning
● Fault
○ Unknown
```

不要大量使用 blinking animation。

若需要 animation，只應在非常明確的 transient state 使用。

---

# 18. Update Rate Strategy

NAV backend 仍以原始資料率接收，例如：

```text
100 Hz
```

但 Web UI 不需要 100 Hz。

建議：

```text
NAV backend processing       raw rate
Numeric UI refresh           ~5 Hz
Map/current position         ~2–5 Hz
Health evaluation            ~5 Hz
```

具體數值可由 agent 調整。

重要原則：

**資料處理頻率與畫面更新頻率分離。**

---

# 19. Product UI

Port：

```text
:8080
```

Navigation：

```text
Overview
Navigation
Data
```

採簡潔 top navigation。

不要使用 engineering-style sidebar。

---

# 20. Product Overview

產品首頁主要回答：

> 現在 AIO Navigation 能不能用？

建議筆電寬螢幕 layout：

```text
┌──────────────────────────────────────────────────────────────┐
│ AIO Navigation System                         ● READY        │
│ Overview          Navigation          Data                   │
├──────────────────────────────────────────────────────────────┤
│ NAV Output  ● Streaming  100 Hz  → 192.168.50.91:9000      │
├───────────────────────────────────┬──────────────────────────┤
│                                   │ POSITION                 │
│                                   │ Lat      22.xxxxxxxx     │
│                                   │ Lon     120.xxxxxxxx     │
│                                   │ Height       xxx.xx m    │
│                                   │                          │
│            LIVE MAP               │ MOTION                   │
│                                   │ Heading       278.4° ↖   │
│     Current position              │ Speed          xx.xx m/s │
│     Heading marker                │                          │
│     Short trajectory              │ ATTITUDE                 │
│                                   │ Roll            x.xx°    │
│                                   │ Pitch           x.xx°    │
├───────────────────────────────────┴──────────────────────────┤
│ Alignment ●   GNSS ●   Camera ●   LiDAR ●   Network ●       │
├──────────────────────────────────────────────────────────────┤
│ Storage xxx GB free                  Uptime xx:xx            │
└──────────────────────────────────────────────────────────────┘
```

---

# 21. Heading Indicator

不要另外做大型 compass widget。

在 Heading 數值旁提供簡單旋轉箭頭即可。

例如：

```text
Heading    278.4°   ↖
```

規則：

```text
0°   = ↑
90°  = →
180° = ↓
270° = ←
```

頁面上方固定代表北。

箭頭可以使用平滑 CSS rotation。

必須處理：

```text
359° → 0°
```

時採 shortest-angle rotation，避免箭頭反向旋轉一整圈。

---

# 22. Product Map

使用 Leaflet。

v1：

- online basemap 有網路時使用
- 沒網路時不要求 basemap 正常
- navigation position / trajectory 本身仍應可運作

不做 offline tile package。

Map：

- North-up
- 不隨 heading 旋轉整張地圖
- current position marker
- marker 帶 heading
- short trajectory on Overview

---

# 23. Navigation Detail Page

Navigation page 採：

**大地圖 + 詳細數值**

不做 trend charts。

建議：

```text
┌──────────────────────────────────────┬───────────────────────┐
│                                      │ POSITION              │
│                                      │ Latitude              │
│                                      │ Longitude             │
│                                      │ Height                │
│                                      │                       │
│             LARGE MAP                │ VELOCITY              │
│                                      │ North                 │
│     Current position                 │ East                  │
│     Heading                          │ Up                    │
│     Full trajectory                  │ Speed                 │
│                                      │                       │
│                                      │ ATTITUDE              │
│                                      │ Roll                  │
│                                      │ Pitch                 │
│                                      │ Heading         ↖     │
├──────────────────────────────────────┴───────────────────────┤
│ Alignment ●  GNSS ●  ZUPT ○  NHC ●  VUPT ○                │
├──────────────────────────────────────────────────────────────┤
│ UDP Streaming | Rate | Freshness | Destination              │
└──────────────────────────────────────────────────────────────┘
```

---

# 24. Trajectory Retention

Trajectory scope：

**本次 `aio_nav_node` runtime/session**

不要永久在 browser 中保存 100 Hz trajectory。

使用 multi-resolution buffer。

v1 建議：

```text
Recent 60 seconds:
10 Hz

Older trajectory:
1 Hz
```

例如一小時：

```text
Recent:
~600 points

Older:
~3540 points
```

瀏覽器負擔合理。

Raw NAV stream 仍維持完整 rate 供 health 判斷。

Trajectory sampling 只影響 visualization。

如果偵測到 `aio_nav_node` restart，可開始新的 trajectory session。

---

# 25. Data Download

Product UI：

```text
/data
```

一般使用者可直接使用。

不需要登入。

v1 scope：

```text
Jetson → Remote PC
```

Read-only。

概念上支援：

- browse approved data location
- file information
- download

暫時不做：

- upload
- delete
- rename
- move
- arbitrary Linux filesystem browsing

Dataset structure、folder schema、archive format：

**v1 規劃階段暫不定義。**

只保留 extensible Data module。

---

# 26. Maintenance UI

Port：

```text
:8081
```

採 engineering-style sidebar：

```text
Overview
System
ROS 2
Camera
LiDAR
Network
Diagnostics
```

Navigation 詳細數值本身已在 Product UI 提供，因此 Maintenance 不需要重做另一套完整 NAV UI。

---

# 27. Maintenance Overview

目標：

> 工程人員約 10 秒內能判斷問題在哪一層。

顯示四個主要層次：

```text
System
Services
Data Flow
Network
```

例如：

```text
SYSTEM
CPU 32%
GPU 41%
RAM 6.2 / 16 GB
Temp 52°C
Disk 38%
Uptime ...

SERVICES
aio_nav      Running
camera       Running
ouster       Running
dashboard    Running

DATA FLOW
NAV UDP      100 Hz
Camera ROS    15 Hz
LiDAR ROS     10 Hz
Ouster IMU   100 Hz

NETWORK
Sensor LAN   Up
Camera       Reachable
Ouster       Reachable

ACTIVE ISSUES
...
```

異常項目應可直接進入相對應 detail page。

---

# 28. System Maintenance Page

v1 顯示：

- CPU usage
- GPU usage
- RAM usage
- disk usage
- free space
- CPU/GPU temperature
- uptime
- Ethernet interface state
- IP address
- systemd service status

不做：

- per-core historical charts
- swap/page fault analytics
- SMART deep diagnostics
- power rails
- fan curves
- kernel metrics
- long-term host telemetry

---

# 29. Camera Maintenance

保持簡單。

例如：

```text
Camera
● Reachable
● Driver Running
● ROS Topic Active

FPS          15.0 Hz
Freshness    24 ms

[ Run Diagnostic ]
[ Restart Driver ]
```

不要求 v1 深入 GigE diagnostics。

---

# 30. LiDAR Maintenance

保持簡單。

例如：

```text
LiDAR
● Reachable
● Driver Running
● Point Cloud Active

Rate         10.0 Hz
Freshness    42 ms

[ Run Diagnostic ]
[ Restart Driver ]
```

可視實際 Ouster driver 增加 IMU topic basic status。

不要求 v1 做 packet-level inspection。

---

# 31. Network Maintenance

v1 顯示：

- Ethernet link state
- interface name
- Jetson IP
- subnet/basic route
- Camera reachability
- LiDAR reachability
- configured NAV UDP destination
- basic device network status

不做完整 network analyzer。

---

# 32. Diagnostics

v1 保持簡單。

內容：

- Active warnings
- Active faults
- Recent events
- Diagnostic action result
- Restart action result

例如：

```text
14:22:31 Camera diagnostic       PASS
14:18:07 Ouster driver restart   SUCCESS
14:10:42 Sensor unreachable      WARNING
```

---

# 33. Actions Architecture

第一版先保留簡單且可擴充的 action interface。

例如：

```text
actions/
├── registry.py
├── diagnostics.py
└── service_control.py
```

允許：

```text
run_diagnostic("camera")
run_diagnostic("ouster")

restart_service("camera")
restart_service("ouster")
```

禁止：

```text
run_shell(user_supplied_command)
```

所有 action：

- 必須來自 whitelist
- 必須回傳明確 success / failure
- 必須有 timeout
- 必須記錄 log
- UI 必須顯示 running / success / failure
- destructive/state-changing operation 應先 confirmation

AIO NAV v1 暫不提供 Stop / Restart button。

---

# 34. Recording

v1 暫不納入正式 scope。

可以預留 backend/action architecture，但不要先實作完整：

- rosbag start/stop
- recording profiles
- dataset management

等真正需求確定後再增加。

---

# 35. Frontend Technology

優先重用 setup_env 已驗證的 offline-compatible frontend stack：

- Bootstrap
- Alpine.js
- Leaflet

不需要為 v1 導入大型 SPA framework，除非實作過程證明有明確必要。

目標：

- offline-capable static assets
- responsive laptop UI
- low dependency complexity
- easy Jetson deployment

---

# 36. Backend Technology

可優先沿用 Python Web backend 的既有經驗，例如 Flask。

但實作重點不是 framework，而是：

- collectors 與 Web route 分離
- state thread-safe
- sensor monitoring 不阻塞 request thread
- ROS / UDP listeners 長時間穩定
- action 有 timeout
- clean startup/shutdown

---

# 37. Testing Strategy

開發機為 x86，不假設有完整 ROS / sensor hardware。

因此應提供 fake/test source。

至少包括：

## Fake NAV UDP sender

可以模擬：

- normal 100 Hz output
- alignment false → true
- GNSS valid → unavailable
- ZUPT/NHC/VUPT switching
- packet pause
- total stream loss
- position movement
- heading crossing 359° → 0°

---

## Fake subsystem state

可模擬：

- Camera reachable/unreachable
- LiDAR reachable/unreachable
- ROS topic healthy/stale/missing
- service running/stopped
- warning/fault

UI 開發不應依賴 Jetson 真機才能進行。

---

# 38. Suggested Implementation Order

## Phase 1 — Project Skeleton

建立：

- Product :8080
- Maintenance :8081
- shared state model
- static assets
- configuration
- basic systemd service

---

## Phase 2 — NAV Core

完成：

- UDP listener
- decoder extraction
- VUPT fix
- velocity_up fix
- current NAV state
- packet rate
- freshness
- product health
- fake sender tests

這是最高優先級。

---

## Phase 3 — Product UI

完成：

- Overview
- Navigation
- online Leaflet map
- heading arrow
- trajectory
- status indicators
- UDP output display

---

## Phase 4 — System + ROS Maintenance

完成：

- CPU/GPU/RAM/disk/temp
- systemd status
- ROS nodes
- ROS topics
- rate
- freshness
- pub/sub counts

---

## Phase 5 — Sensor Maintenance

完成：

- Camera basic reachability
- Camera ROS health
- Ouster basic reachability
- Ouster ROS health

---

## Phase 6 — Network / Diagnostics / Actions

完成：

- NIC/basic network
- warnings/events
- simple diagnostics
- whitelisted driver restart

---

## Phase 7 — Data Download Skeleton

建立 read-only Data module。

先完成安全、乾淨的下載路徑。

Dataset organization 暫不深入設計。

---

# 39. v1 Explicit Non-Goals

Coding agent 不應自行加入下列內容：

- complete ROS debugging suite
- rqt clone
- ROS graph visualization
- message payload browser
- full sensor raw stream duplicated into Dashboard
- offline map tile server
- user account system
- OAuth
- RBAC
- arbitrary shell
- general-purpose Linux file manager
- upload
- file deletion
- full recording workflow
- historical Prometheus/Grafana-style telemetry
- advanced IMU diagnostics
- advanced Camera diagnostics
- packet-level Ouster diagnostics
- complex animations
- 3D attitude indicator
- artificial horizon
- editable NAV destination configuration

除非後續明確提出需求。

---

# 40. Design Principle

整個 v1 應遵守：

**Product UI**
回答：

> AIO NAV 現在能不能使用？資料在哪裡？UDP 有沒有正常輸出？

**Maintenance UI**
回答：

> 如果有異常，是 system、service、ROS、sensor 還是 network 哪一層？

不要把這兩個目的混在同一個畫面。

第一版優先：

- simple
- stable
- readable
- diagnosable
- extensible

而不是功能最大化。

未來 Research / Development version 可以沿用相同 collector / state / action architecture，再增加更複雜的操作與診斷功能。