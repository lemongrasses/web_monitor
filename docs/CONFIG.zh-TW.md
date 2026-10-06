# AIO System Dashboard 設定手冊

[English version](CONFIG.md)

本手冊說明儀表板的設定檔放在哪裡、如何安全地修改，以及每一項設定的意義。

**重點摘要：** 隨附的設定檔不需修改即可使用。大部分設定值為 `auto`（啟動時自動偵測）或屬於選填。通常只需要填入相機與光達的 IP 位址，或在自動偵測結果不正確時手動指定。

---

## 1. 設定檔的位置

安裝程式會把程式複製到 `/opt/aio-dashboard`。執行中的儀表板**只會讀取**這個檔案：

```
/opt/aio-dashboard/config/dashboard.yaml
```

下載下來的程式碼裡的那一份（`~/web_monitor/config/dashboard.yaml`）只是範本，只在第一次安裝時被複製到 `/opt/aio-dashboard`。之後再修改它不會有任何效果，除非用 `--reset-config` 重新安裝（見下文）。

| 檔案 | 執行中的儀表板會讀取嗎？ |
|------|------------------------|
| `/opt/aio-dashboard/config/dashboard.yaml` | **會** |
| `~/web_monitor/config/dashboard.yaml` | 不會，僅作為新安裝的範本 |
| `/opt/aio-dashboard/config/dashboard.yaml.new` | 不會，更新時保留您的設定後所附的最新範本 |
| `/opt/aio-dashboard/config/dashboard.yaml.bak` | 不會，`--reset-config` 備份的舊設定 |

## 2. 如何修改設定

1. 編輯已安裝的設定檔：
   ```bash
   sudo nano /opt/aio-dashboard/config/dashboard.yaml
   ```
   （也可以執行 `aio-dashboard config`，一次完成開啟編輯器、檢查設定檔與詢問是否重新啟動。）
2. 重新啟動儀表板（設定只在啟動時讀取）：
   ```bash
   sudo systemctl restart aio-dashboard
   ```
3. 確認是否正常啟動，並查看偵測結果：
   ```bash
   systemctl status aio-dashboard --no-pager
   journalctl -u aio-dashboard -n 40 --no-pager
   ```
   也可以打開維護頁面（`http://<jetson-ip>:8081`）：**Network** 頁面會顯示偵測到的 `aio_nav.yaml` 路徑與網路介面，**ROS 2** 頁面會顯示選用的 topic。

**重新啟動權限需要多一個步驟：** 如果修改了哪些驅動程式服務可以重新啟動（`services` 底下的 `restartable` 或 `unit`），也必須重新執行一次安裝程式。允許重新啟動的 sudo 權限是依據已安裝的設定檔產生的：
```bash
cd ~/web_monitor && sudo deploy/install.sh --user jetson
```

### 新裝置的兩種設定方式

- **先安裝，再調整（建議）。** 執行安裝程式，之後只有在需要時才編輯 `/opt/aio-dashboard/config/dashboard.yaml`。
- **先編輯，再安裝。** 在第一次安裝前編輯 `~/web_monitor/config/dashboard.yaml`，安裝時會被複製進去。若裝置上已經裝過儀表板，執行 `sudo deploy/install.sh --user jetson --reset-config` 以您編輯的範本覆蓋已安裝的設定，舊的會保留為 `dashboard.yaml.bak`。

更新程式碼（`git pull` 後執行安裝程式，不加 `--reset-config`）永遠不會覆蓋您已安裝的設定。

## 3. 需要注意的 YAML 規則

- 一律用**空白**縮排，不可使用 Tab。子項目要縮排在上層項目之下。
- 省略的數值與選項會使用內建預設值。但 `services`、`devices`、`ros.topics` 與 `ros.nodes` **沒有**內建項目：刪除這些區段就等於移除那些項目。請直接修改隨附的設定檔，不要從頭寫一份新的。
- **清單會整個取代預設值。** 如果寫了 `ros.topics`，就要寫出所有需要的 topic，而不是只寫新增的那一個。`ready_requires`、`nodes`、`disk_paths` 也一樣。
- 含有 `:` 或 `#` 的文字請加上引號，例如 `host: "192.0.2.20"`。
- `auto` 是關鍵字，代表該項設定使用自動偵測。

設定檔有語法錯誤時儀表板無法啟動，systemd 會每隔幾秒重試一次。可用以下指令檢查設定檔：
```bash
python3 -c "import yaml; yaml.safe_load(open('/opt/aio-dashboard/config/dashboard.yaml')); print('OK')"
```

相對路徑（例如 `logs/events.jsonl`）是相對於 `/opt/aio-dashboard`。

---

## 4. 設定項目說明

### 4.1 `product` 與 `maintenance`：兩個網頁

```yaml
product:      { host: 0.0.0.0, port: 8080 }
maintenance:  { host: 0.0.0.0, port: 8081 }
```

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `host` | `0.0.0.0` | 監聽的網路位址。`0.0.0.0` = 所有網路介面，其他電腦可以開啟頁面；`127.0.0.1` = 只有這台 Jetson 本身可以開啟。 |
| `port` | `8080` / `8081` | 產品頁面／維護頁面的連接埠。 |

產品頁面給操作人員使用；維護頁面給工程人員使用，且可以重新啟動驅動程式。若希望維護頁面只能從 Jetson 本機開啟，請設定 `maintenance.host: 127.0.0.1`。

### 4.1a `access`:誰可以開啟網頁

```yaml
access:
  allowed_clients: [192.168.116.154]
```

| 設定 | 預設 | 說明 |
|-----|------|------|
| `allowed_clients` | `[]` | 允許開啟 Product / Maintenance 頁面的電腦 IP 或 CIDR(例如 `192.168.116.0/24`)。其他來源會得到 `403 Forbidden`。本機 `127.0.0.1` 一律允許。留空代表不限制。 |

預設值是感測器網段上的外部電腦(`192.168.116.154`);本 Jetson 在 `eno1` 上是 `192.168.116.1`。這只是來源 IP 過濾,不是身分驗證,無法防止同一網段內有人偽造該 IP。

### 4.2 `nav`：AIO NAV 資料與 Ready／Initializing／Fault 狀態

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `udp_bind` | `127.0.0.1:9000` | 儀表板接收 AIO NAV 封包的位址。`aio_nav_node` 一律送往 `127.0.0.1:9000`，請勿修改。只有儀表板在監聽:AIO Nav 桌面程式(`aio-nav-ui`)綁定同一個埠,所以不要與儀表板同時使用。 |
| `aio_nav_config` | `auto` | AIO NAV 的 `aio_nav.yaml` 路徑。儀表板從中讀取 UDP 目的地（`output_udp`）與輸出頻率（`output_rate`）來顯示。`auto` 會搜尋 `/home/*/aio-nav-ros/install/...` 與 `~/.local/opt/aio-nav-ros/...`，並使用最新的檔案；也可以用環境變數 `AIO_NAV_CONFIG` 指定。 |
| `expected_rate_hz` | `null` | 預期的封包頻率。`null` = 使用 `aio_nav.yaml` 中的 `output_rate`。 |
| `service` | `aio_nav` | `services` 中哪一個項目代表 AIO NAV。 |
| `allow_control` | `false`(預設設定檔:`true`) | `true` 時,首頁會顯示 AIO NAV 的 **Start / Restart / Stop**。**Start** 會先啟動濾波器再啟動 DSO,**Stop** 兩者一起停止,做法與 AIO Nav 桌面程式相同(程序已在執行就不會重複啟動,所以不會跟桌面程式啟動的那份重複)。啟動的是 aio-nav-ros `install/` 資料夾內的 `aio-nav` 與 `aio-nav-dso`,不需要原始碼。Stop 與 Restart 需要再按一次確認。只有 `access.allowed_clients` 內的電腦連得到頁面。 |
| `control_group` | `[aio_nav, dso]` | Start/Stop 作用在 `services` 的哪些項目,依序啟動。每個項目都需要設定 `launch`。 |
| `startup_grace_s` | `3.0` | 儀表板啟動後的這段時間內，沒有封包會顯示「Unknown」而不是「Fault」。 |
| `stale_warn_s` | `0.5` | 封包超過這個秒數未更新時，UDP 輸出顯示 **Stale**。 |
| `stale_fault_s` | `2.0` | 封包超過這個秒數未更新時，UDP 輸出顯示 **Lost**，狀態變成 **Fault**。 |
| `low_rate_ratio` | `0.8` | 實測頻率低於預期頻率的這個比例時，UDP 輸出顯示 **Low rate**。 |
| `gnss_timeout_s` | `3.0` | 超過這段時間沒有 GNSS 更新時，GNSS 顯示 **Unavailable**。這只是提示，永遠不會讓 Ready 變成 Fault。 |
| `flag_active_s` | `1.0` | 濾波器最後一次使用 ZUPT、ZIHR、NHC、VUPT 之後，對應指示燈保持 **Active** 的時間。 |
| `ready_requires` | `[alignment, heading_valid, fine_alignment]` | 必須全部成立才算 **Ready** 的濾波器旗標，在此之前狀態為 **Initializing**。可用值：`alignment`、`heading_valid`、`fine_alignment`。 |
| `trajectory.recent_window_s` | `60` | 以完整細節顯示的最近軌跡長度（秒）。 |
| `trajectory.recent_hz` | `10` | 最近軌跡每秒保留的點數。 |
| `trajectory.older_hz` | `1` | 較早軌跡每秒保留的點數。 |

整體狀態的判斷方式：

- **Fault**：AIO NAV 沒有在執行，或超過 `stale_fault_s` 秒沒有收到封包。
- **Initializing**：有收到封包，但 `ready_requires` 的旗標還沒有全部成立。
- **Ready**：AIO NAV 正在執行、封包即時，且對準（alignment）已完成。

GNSS、相機、光達與網路只會產生提示，永遠不會讓 Ready 變成 Fault。狀態要改變，條件必須持續約 0.5–1 秒；從異常恢復到 Ready 則必須穩定約 2 秒。這些時間是內建的，無法設定。

> 總覽頁面上的「Sending to …」位址**不是**在這裡設定的，而是來自 AIO NAV 的 `aio_nav.yaml` 中的 `output_udp`。請在那裡修改，然後重新啟動 AIO NAV 與儀表板。

### 4.3 `services`：要監看（及重新啟動）的 systemd 服務

```yaml
services:
  aio_nav:   { label: AIO NAV, unit: aio-nav.service, process_pattern: lib/aio_nav_ros/aio_nav_node }
  camera:    { label: Camera driver, unit: camera.service, restartable: true }
  ouster:    { label: Ouster driver, unit: ouster.service, restartable: true }
  dashboard: { label: Dashboard, unit: aio-dashboard.service }
```

| 項目 | 說明 |
|------|------|
| （項目名稱） | 內部名稱，例如 `camera`。`devices.<名稱>.service` 與 `nav.service` 會用到它。 |
| `label` | 介面上顯示的名稱。 |
| `unit` | systemd 服務名稱（`systemctl status <unit>`）。 |
| `process_pattern` | 選填。若有程序符合這段文字（`pgrep -f`），也視為正在執行。AIO NAV 使用這個設定，因此從 AIO Nav 桌面應用程式啟動也能偵測到。 |
| `restartable` | `true` 會在相機或光達頁面加上 **Restart driver** 按鈕。修改後請重新執行安裝程式（見第 2 節）。 |
| `launch` | aio-nav-ros `install/aio_nav_ros/lib/aio_nav_ros/` 資料夾內的啟動程式名稱(`aio-nav`、`aio-nav-dso`),或絕對路徑。設定後儀表板可以啟動與停止這個程式,並用 `process_pattern` 找到它。程式以儀表板的使用者身分在獨立的 session 中執行,ROS 網域由 `aio_nav.yaml` 決定,不是儀表板的設定。輸出寫到 `logs/<name>.log`。儀表板服務使用 `KillMode=process`,所以重啟儀表板時 AIO NAV 不會被停掉。 |
| `optional` | `true` 代表這個程式停止時不算警告(DSO 使用)。 |

裝置上不存在的服務會顯示 **Not installed**，不會產生警告，所以用不到的項目放著也沒關係。查詢實際的服務名稱：
```bash
systemctl list-units --type=service | grep -iE "camera|ouster|lidar|ros"
```
只有這個檔案中列出的 `unit` 可以被重新啟動，網頁無法執行任何其他指令。

### 4.4 `devices`：相機與光達

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

| 項目 | 說明 |
|------|------|
| （項目名稱） | 請保留 `camera` 與 `lidar`，維護頁面正是依這兩個名稱提供頁面。 |
| `label` | 指示燈與頁面上顯示的名稱。 |
| `host` | 選填。感測器的 IP 位址，每隔 `network.ping_interval_s` 秒用 ping 檢查一次。留空 = 顯示 **Not set up**，不會 ping，也不會有警告。 |
| `service` | `services` 中哪一個項目是這個感測器的驅動程式。 |
| `topic_group` | `ros.topics` 中哪些項目屬於這個感測器（對應其 `group`）。 |
| `preview.kind` | 相機用 `image`，光達用 `pointcloud`。不需要即時畫面就省略 `preview`。 |
| `preview.topic` | **Live view** 顯示的 ROS topic。`auto` = 第一個 `CompressedImage` 或 `Image` topic（相機，會略過深度影像），或 `PointCloud2` topic（光達）。也可以填入完整名稱，例如 `/ouster/points`。 |
| `preview.hint` | 選填。自動選擇時，topic 名稱最好包含的文字，例如 `color`。 |
| `preview.max_width` | 相機：未壓縮的影像會先縮小到這個寬度再傳送；壓縮影像則原樣傳送。 |
| `preview.max_points` | 光達：點雲會隨機抽樣到這個點數再傳給瀏覽器。 |

Live view 只在相機或光達頁面開啟時才讀取 topic，離開頁面 10 秒後停止。

查詢感測器 IP 與 topic：
```bash
ros2 topic list -t                 # 列出 topic 與訊息型別
ros2 param get /ouster/os_driver sensor_hostname   # Ouster IP（節點名稱可能不同）
```

### 4.5 `network`：網路

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `sensor_interface` | `auto` | 連接感測器網路的網路介面，例如 `eth0`。`auto` = 通往裝置 `host` 的介面，若沒有則使用預設路由的介面。顯示為 **Network** 指示燈（連線中或中斷）。可用 `ip -br link` 列出所有介面。 |
| `ping_interval_s` | `5` | 每次 ping 裝置 `host` 之間的秒數。 |

### 4.6 `system`：Jetson 系統狀態

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `interval_s` | `2` | 讀取 CPU、GPU、記憶體、磁碟與溫度的間隔。 |
| `disk_paths` | `["/"]` | 要回報的磁碟；資料資料夾會自動加入。 |
| `disk_warn_percent` | `90` | 磁碟使用率（%）達到此值時，發出「Storage almost full」警告。 |
| `temp_warn_c` | `85` | 溫度（°C）達到此值時，發出「Temperature high」警告。 |

### 4.7 `ros`：ROS 2 監看

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

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `enabled` | `true` | `false` 會關閉 ROS 2 監看，topic 指示燈會顯示 Unknown。 |
| `mode` | `null`(預設設定檔:`live`) | 在維護頁面 **ROS 2** 另外選擇之前使用的 ROS 環境預設組。`null` 表示沿用服務的環境變數。在維護頁面選的組合存在 `state/ros_mode.json`(優先於這個設定),會套用到儀表板自己的 ROS 連線,以及從首頁啟動的 AIO NAV 與 DSO。 |
| `modes` | `live`:網域 10、僅本機;`bag`:網域 13、網路 | 預設組(`label`、`domain_id`、`localhost_only`)。**Live** 與相機、IMU 驅動一致;只有播放 bag 時才用 **Bag replay**。在維護頁面切換會停止 AIO NAV 與 DSO 並重啟儀表板(幾秒鐘),之後需要再次啟動 AIO NAV。相機與 IMU 驅動不受影響。 |
| `graph_interval_s` | `2` | 重新整理節點與 topic 清單的間隔。 |
| `rate_window_s` | `2` | 計算每個 topic 頻率所用的時間窗。 |
| `nodes` | `[]` | 必須存在的節點名稱（顯示於 ROS 2 頁面，缺少時會產生警告）。 |
| `topics` | `[]` | 要量測頻率與即時性的 topic（見下表）。 |

`topics` 中每個項目的欄位：

| 欄位 | 說明 |
|------|------|
| `name` | 完整的 topic 名稱，或用 `auto` 依 `type` 自動選擇。 |
| `type` | 訊息型別，使用 `auto` 時必填，例如 `sensor_msgs/msg/PointCloud2`。 |
| `hint` | 使用 `auto` 時，優先選擇名稱包含這段文字的 topic。 |
| `strict` | 使用 `auto` 時，`true` = **只**接受名稱包含 `hint` 的 topic。例如可避免「Ouster IMU」誤選到導航用的 IMU。 |
| `group` | `nav`、`camera` 或 `lidar`，把 topic 對應到感測器頁面與其診斷。 |
| `label` | 介面上顯示的名稱。 |
| `expected_hz` | 選填，預期頻率。低於一半時顯示 **Low rate**，也決定多快會被判定為 Stale。 |

Topic 狀態：

| 狀態 | 意義 | 是否警告 |
|------|------|----------|
| Active | 訊息以正常頻率到達 | 否 |
| Low rate | 頻率低於 `expected_hz` 的 50% | 是 |
| Stale | 有發布者，但超過 max(1 秒, 5 ÷ `expected_hz`) 沒有訊息；未設定頻率時為 3 秒 | 是 |
| Missing | 指定名稱的 topic 不存在或沒有發布者 | 是 |
| Not found | `auto` 找不到任何符合的 topic，通常代表沒有安裝這個感測器 | 否 |

監看使用 raw 訂閱，只計數、不解碼訊息。長時間監看請盡量選擇輕量的 topic（例如用 `camera_info` 而非 `image_raw`）。

### 4.7a `dso_watchdog`:DSO 里程計壞掉時自動重啟

```yaml
dso_watchdog:
  enabled: true
  topic: /dso/odometry
  service: dso
```

DSO 追蹤失敗時,`/dso/odometry` 會變成 NaN,必須重啟 DSO。監控程式會看這個 topic,並且**只重啟 DSO**(與 `restart_process dso` 相同),AIO NAV 不受影響。首頁沒有任何操作,狀態在維護頁面的 ROS 2,每次重啟都會記錄在事件紀錄。

| 設定 | 預設 | 說明 |
|-----|------|------|
| `enabled` | `false`(預設設定檔:`true`) | 開啟監控。 |
| `topic` | `/dso/odometry` | 要監看的 `nav_msgs/Odometry`。位置或速度中有任何 NaN 或無限大就算異常。 |
| `service` | `dso` | 要重啟的 `services` 項目(需設定 `launch`)。 |
| `bad_messages` | `3` | 連續幾筆異常才重啟,單筆雜訊不會觸發。 |
| `settle_s` | `15` | 重啟後給 DSO 初始化的時間,這段時間不判斷。 |
| `cooldown_s` | `30` | 兩次重啟的最短間隔。 |
| `max_restarts` / `window_s` | `5` / `600` | 時間窗口內重啟達此次數後不再重啟,狀態顯示 **Gave up**,直到里程計恢復正常。 |

只在 DSO 程序執行中才會動作,所以你主動停掉的 DSO 不會被拉起來。

### 4.8 `data`：資料下載頁面

```yaml
data:
  roots: auto
```

`auto` 會提供 AIO NAV 的紀錄資料夾：若 `aio_nav.yaml` 的 `fusion_txt_path` 是絕對路徑，就用它所在的資料夾；否則使用 aio-nav-ros 的 `install/` 旁邊的 `output/`。也可以自行列出資料夾：

```yaml
data:
  roots:
    - { id: logs,  name: AIO NAV logs, path: /home/jetson/aio-nav-ros/output }
    - { id: bags,  name: ROS bags,     path: /home/jetson/bags }
```

| 欄位 | 說明 |
|------|------|
| `id` | 簡短且不重複的名稱，用於下載連結。 |
| `name` | 資料頁面上顯示的名稱。 |
| `path` | 要提供的資料夾。下載範圍無法超出這個資料夾。頁面為唯讀：無法上傳、刪除或重新命名檔案。 |

### 4.9 `events`：事件紀錄

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `log_file` | `logs/events.jsonl` | 保存狀態變化、診斷與重新啟動紀錄的檔案，超過 5 MB 會輪替。 |
| `max_memory` | `500` | Diagnostics 頁面可顯示的最近事件數量。 |

### 4.10 `actions`：維護操作

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `restart_timeout_s` | `30` | 驅動程式重新啟動超過這個時間會被回報為失敗。 |
| `diagnostic_timeout_s` | `15` | **Run diagnostic** 的逾時時間。 |

### 4.11 `fake`：僅供展示／開發

| 項目 | 預設值 | 說明 |
|------|--------|------|
| `enabled` | `false` | `true` 會改用 `state_file` 中模擬的服務、網路與 ROS 資料，而不是真實資料。**實機上請維持 `false`。** |
| `state_file` | `dev/fake_state.json` | 模擬狀態檔，執行中也可以修改。 |

---

## 5. 常見操作

**填入相機與光達的 IP 位址。** 在現有的 `devices` 區段中填入 `host` 這一行，其他行保持不變：
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

**在 Live view 顯示指定的相機串流。** 修改 `devices.camera` 中的 `preview` 這一行：
```yaml
    preview: { kind: image, topic: /camera/color/image_raw/compressed, max_width: 960 }
```

**使用自己的驅動程式服務名稱，讓重新啟動按鈕可以運作。** 修改 `services` 中 `camera` 與 `ouster` 這兩行：
```yaml
services:
  camera: { label: Camera driver, unit: usb-cam.service, restartable: true }
  ouster: { label: Ouster driver, unit: ouster-driver.service, restartable: true }
```
接著重新執行 `sudo deploy/install.sh --user jetson`。

**預設三顆對準燈（Alignment、Initial heading、Fine alignment）都亮才算 Ready。** 若要放寬，修改 `nav` 中的 `ready_requires` 這一行：
```yaml
nav:
  ready_requires: [alignment, heading_valid, fine_alignment]
```

**aio-nav-ros 安裝在非預設位置。** 修改 `nav` 中的 `aio_nav_config` 這一行：
```yaml
nav:
  aio_nav_config: /data/aio-nav-ros/install/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml
```

任何修改之後：`sudo systemctl restart aio-dashboard`。

## 6. 問題排除

| 狀況 | 可能原因與解決方式 |
|------|------------------|
| 修改後沒有效果 | 改到的是 `~/web_monitor/config/dashboard.yaml` 而不是 `/opt/aio-dashboard/config/dashboard.yaml`，或忘了執行 `sudo systemctl restart aio-dashboard`。 |
| 修改後頁面打不開 | YAML 語法錯誤。執行第 3 節的檢查指令，並查看 `journalctl -u aio-dashboard -n 40`。 |
| 「Sending to」顯示錯誤的位址 | 這個位址來自 `aio_nav.yaml` 的 `output_udp`，不是這個設定檔（見 4.2）。 |
| Network 頁面顯示找不到 `aio_nav.yaml` | 將 `nav.aio_nav_config` 設為完整路徑（見第 5 節）。 |
| ROS 2 頁面顯示 rclpy 無法使用 | `/opt/ros/humble` 沒有安裝 ROS 2 Humble，服務無法載入 ROS 2。 |
| Live view 顯示「no PointCloud2 topic found」 | 沒有符合的 topic 正在發布。用 `ros2 topic list -t` 檢查，或設定 `preview.topic`。 |
| 相機／光達顯示「Not set up」 | 沒有設定 `host`。若不需要 ping 檢查，這是正常的。 |
| Restart driver 出現 sudo 錯誤 | 安裝後更改過服務名稱，請重新執行安裝程式（見第 2 節）。 |
