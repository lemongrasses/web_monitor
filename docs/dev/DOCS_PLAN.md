# 文件重整計畫(對照表與大綱)

把現有的手冊依讀者重新分成幾份文件。這份是計畫,確認後才開始寫內文。

最後更新:2026-11-19

## 0. 已決定

- **英文為主**,繁體中文版在英文定稿後翻譯(依 [TERMINOLOGY.zh-TW.md](../TERMINOLOGY.zh-TW.md))。
- **Markdown**,放在 repo 的 `docs/`。
- **內部文件放 `docs/dev/`**(方向、功能盤點、想法、流程圖、本計畫、v1 規格),不放進發行包。

## 1. 新的文件組成

| 檔案 | 標題 | 讀者 | 一句話 |
|------|------|------|--------|
| `docs/USER_GUIDE.md` | User Guide | 採數據的人、一般使用者 | 打開頁面、看懂狀態、採一次資料、拿走資料 |
| `docs/MAINTENANCE.md` | Maintenance Guide | 現場工程師 | 用維護頁找出原因並處理;依症狀查 |
| `docs/INSTALL.md` | Installation Guide | 部署的人 | 安裝、更新、移除、選配部分、第一次設定、安全 |
| `docs/CONFIG.md` | Configuration Reference | 部署的人、工程師 | 每個設定項目(查表用,不教流程) |
| `docs/INTEGRATION.md` | Integration Guide | 接收導航資料的軟體、車輛整合者 | Part A 導航資料(0x04、ROS、紀錄檔);Part B 給車輛的就緒訊號(0x05) |
| `docs/SYSMON.md` | System Recorder | 工程師 | 維持現狀(已經是單一主題),只修連結 |
| `README.md` | — | 所有人、開發者 | 是什麼、文件索引、開發快速開始;其他搬走 |
| `docs/dev/ARCHITECTURE.md` | Architecture | 開發者 | 程式結構、分層、健康模型、模組規則、測試 |

**Integration Guide 分兩部分的原因:** Part B(0x05)對應程式的 `vehicle/` 層,之後輸出拔到其他專案時,Part B 可以整段帶走。

**舊檔案的去向:** `OPERATION.md` / `OPERATION.zh-TW.md` 的內容全部分到上面幾份後刪除;`CONFIG.md` 留下設定參考的部分,其餘搬到 INSTALL。舊的中文版在新的中文版完成前先保留,開頭加一行「舊版,將被取代」。

## 2. 對照表:現有內容 → 新文件

動作:**搬**(大致照搬)、**改寫**(內容對,但要依新讀者重寫)、**刪**(重複或過時)。

### OPERATION.md(951 行)

| 現在的章節 | 去向 | 動作 | 說明 |
|------------|------|------|------|
| 開頭、1. What the system provides | USER_GUIDE 1 | 改寫 | 表格裡維護頁、`aio-dashboard` 指令移到 MAINTENANCE;「全自動」那段縮成兩三句 |
| 2. Opening the dashboard | USER_GUIDE 2 | 搬 | 熱點連線方式也放這裡(使用者第一次連線就會用到) |
| 3.1 The status area | USER_GUIDE 3 | 改寫 | 依新頁面:一句話標題、依環境換說法 |
| 3.2 Start-up progress | USER_GUIDE 3 | 改寫 | 「步驟」一節:每個步驟一行,顏色意義一張表 |
| 3.3 Status dots | USER_GUIDE 3 | 改寫 | 只留使用者頁看得到的;維護頁燈號移 MAINTENANCE |
| 3.4 Pages | USER_GUIDE 4 | 搬 | |
| 3.5 Settings page | USER_GUIDE 4 | 搬 | 環境卡片、Wi-Fi、自動啟動、車端連線 |
| 3.6 Setup page: lever arms | USER_GUIDE 4 | 搬 | 量測方式、圖 |
| 4.1–4.5 Daily operation | USER_GUIDE 5 | 改寫 | 改成「採一次資料」的完整流程(開機→等就緒→行駛→確認記錄→收工→下載) |
| 4.2 自動啟動的細節(時序、重試、靜止條件) | MAINTENANCE 3 | 搬 | 使用者只需要知道「開機會自己啟動」 |
| 4.6 Replaying a recorded bag | MAINTENANCE 6 | 搬 | 工程師功能 |
| 5. Alignment SOP | USER_GUIDE 6 | 搬 | 5.3「對準出問題」的進階部分移 MAINTENANCE |
| 6.1–6.7 UDP 0x04 | INTEGRATION Part A | 搬 | 原本就寫給整合者 |
| 6.8 Status packet 0x05 | INTEGRATION Part B | 搬 | 含標準封包(golden vector)、vehicle gate 規則 |
| 7.1 ROS 2 topics | INTEGRATION Part A | 搬 | |
| 7.2 Log files | USER_GUIDE 7(下載)+ INTEGRATION Part A(檔案格式) | 拆 | 使用者:怎麼下載;整合者:欄位與格式 |
| 8. Maintenance view | MAINTENANCE 2 | 改寫 | 每頁一小節:看什麼、能做什麼 |
| 8.1 Still detection | MAINTENANCE 4 | 搬 | |
| 8.2 Wi-Fi hotspot | INSTALL(啟用)+ USER_GUIDE 2(連線)+ MAINTENANCE(開關) | 拆 | |
| 9. Troubleshooting | USER_GUIDE 8 + MAINTENANCE 7 | 拆 | 使用者看得到、自己能處理的(約 10 列)留 USER_GUIDE;其餘依症狀分類放 MAINTENANCE |
| 10. Glossary | USER_GUIDE 附錄 | 搬 | 其他文件連過來,不重複 |

### CONFIG.md(799 行)

| 現在的章節 | 去向 | 動作 | 說明 |
|------------|------|------|------|
| 1. Where the configuration file is | CONFIG 1 | 搬 | |
| 2. How to change a setting | CONFIG 2 | 搬 | |
| 2. Two ways to set up a new device / What the installer changes / Optional parts | INSTALL | 搬 | 安裝相關 |
| 3. YAML rules that matter | CONFIG 3 | 搬 | |
| 4. Setting reference | CONFIG 4 | 搬 | 每節開頭補一句「什麼時候需要改」 |
| 4.2a 的「環境」說明 | USER_GUIDE 4(看得懂)+ CONFIG(規則) | 拆 | |
| 5. Common tasks | INSTALL「第一次設定」+ CONFIG 5 | 拆 | 新裝置要做的放 INSTALL,其餘留 |
| 6. Troubleshooting | MAINTENANCE 7 | 搬 | 設定檔錯誤等 |

### README.md(302 行)

| 現在的章節 | 去向 | 動作 |
|------------|------|------|
| 開頭兩張表、全自動說明 | README(縮短)| 改寫 |
| 文件連結 | README「Documentation」索引 | 改寫 |
| Architecture、Health model、Decoder fixes、Live view | dev/ARCHITECTURE | 搬 |
| Development | README | 搬 |
| Deployment(發行包、原始碼安裝、選配、`aio-dashboard` 指令、auto 偵測) | INSTALL | 搬 |
| Security notes | INSTALL「Security」 | 搬 |
| Not included | dev/ARCHITECTURE | 搬 |
| Optional parts、模組規則 | INSTALL(使用者面)+ dev/ARCHITECTURE(模組寫法) | 拆 |

## 3. 大綱

### USER_GUIDE.md — User Guide

1. **What this is** — the device, the dashboard, what you can do with it (3–5 sentences)
2. **Connecting** — by cable, over the hotspot, the address, language
3. **Reading the status** — the headline, the steps and their colours, the environment chip
4. **The pages** — Overview, Navigation, Data, Setup (lever arms), Settings (environment, Wi-Fi)
5. **Collecting data, step by step** — power on → wait for Ready → drive → check Recording → stop → download
6. **Alignment** — the three stages and what to do in each
7. **Getting the data** — the Data page, file names, what is in each file (link to Integration for formats)
8. **Common situations** — what you see, what it means, what you can do (only what a user can do)
- Appendix: Glossary

### MAINTENANCE.md — Maintenance Guide

1. **Before you start** — signing in, the password, `maintenance.lock`
2. **The Maintenance pages** — one short section per page: what it shows, what it can change
3. **How the system runs on its own** — automatic start, the watchers (who restarts what), the operator hold
4. **Still detection** — measuring and setting the limits
5. **Network** — Wi-Fi, the hotspot, the relay
6. **Bag replay** — Live / Bag switch, the player (replay part only)
7. **Troubleshooting by symptom** — grouped: the pages, AIO NAV, sensors and GNSS, network, storage, settings
8. **The `aio-dashboard` command** — status, logs, config, check, password
9. **After a freeze or a power cut** — link to the System Recorder

### INSTALL.md — Installation Guide

1. **What you need** — device, OS, ROS 2, aio-nav-ros
2. **Installing** — release package (recommended) or source checkout
3. **Optional parts** — what each adds, how to switch it on and off, what the release package contains
4. **First-time setup** — password, allowed computers, camera / LiDAR address, environment, checking it works
5. **What the installer changes on the system** — units, sudoers, chrony, sysctl, polkit
6. **Updating and removing**
7. **Security** — the closed sensor LAN, passwords, what each page may change

### CONFIG.md — Configuration Reference

1. **Where the file is**
2. **Changing a setting and checking it**
3. **YAML rules that matter**
4. **Settings, section by section** — the current 4.1–4.12, each opening with "when you need this"
5. **Common changes** — the recipes that are not first-time setup

### INTEGRATION.md — Integration Guide

- **Part A: Navigation data**
  1. Outputs at a glance — UDP 0x04, ROS 2 topics, log files
  2. UDP 0x04 — destination, packet layout, fields, flags, a receiver example, checking it
  3. ROS 2 topics
  4. Log files — names and formats
- **Part B: Readiness for the vehicle (0x05)**
  5. When the vehicle may use the navigation data — the rule in one paragraph
  6. The 0x05 packet — layout, states, actions, health and inhibit bits, the golden vector
  7. The reference receiver (vehicle gate) — rules, `tools/vehicle_gate.py`
  8. Security of the link

### README.md

1. What it is (one paragraph, the two pages)
2. Documentation — the table in section 1 of this plan
3. Development quick start (fake mode, tests)
4. Repository layout (one short tree, link to dev/ARCHITECTURE)

### docs/dev/ARCHITECTURE.md

1. Layers and packages (core, vehicle/, modules) and the rule between them
2. The health step, readiness, environment
3. Collectors, actions, watchers (link to FLOWS)
4. Decoder notes (the setup_env fixes)
5. Optional modules: how to write one
6. Tests and the documentation checks
7. Not included

## 4. 寫作順序

| 順序 | 文件 | 原因 |
|------|------|------|
| 1 | USER_GUIDE | 讀者最多、最短,也最能驗證新的使用者頁 |
| 2 | MAINTENANCE | 接著現場最需要的 |
| 3 | INTEGRATION | 大多是搬;Part B 等 0x05 的待定事項決定後再定稿 |
| 4 | INSTALL + CONFIG | 大多是搬與拆 |
| 5 | README + dev/ARCHITECTURE | 最後收尾,索引要等其他文件定稿 |
| 6 | 中文版 | 英文定稿後依序翻譯;舊中文版到時再刪 |

每份文件:**大綱確認 → 寫內文 → 在模擬模式逐步驗證、重拍截圖 → 請該讀者試讀**。

## 5. 寫作規則

- 每份文件開頭一句:寫給誰、讀完能做什麼。
- 以「要做的事」為單位,不以頁面或程式為單位;一個步驟一句話。
- 同一件事只寫在一個地方,其他文件用連結。
- UI 文字照畫面上的字;英文版用英文介面、中文版用中文介面的字。
- 截圖用可重複執行的腳本產生(待做:`tools/docs_screenshots`),在能上網的機器拍,地圖才有底圖。

## 6. 連帶要改的地方

- `deploy/install.sh`、`tools/build_release.sh`:目前複製 `docs/CONFIG*.md`、`docs/OPERATION*.md`,改成新檔名。
- 測試:`tests/test_status_packet.py` 檢查手冊裡的標準封包(會移到 INTEGRATION);`tests/test_i18n.py` 檢查中文手冊用字(`docs/*.zh-TW.md`)。
- 程式註解裡的「OPERATION.md 6.8」「CONFIG.md 4.x」等引用。
- `TERMINOLOGY.zh-TW.md` 和 aio-nav-receiver 共用,檔名與位置不動。

## 7. 待定事項(會影響內容)

- **0x05 在沒接車輛、Bag 重播時送不送**:影響 INTEGRATION Part B 與 MAINTENANCE 6。先寫現狀,標「待定」。
- **測試機 / 開發機的分界**:影響 INSTALL 的「選配部分」建議組合。
- **沒有 RTK 時的時間限制**:影響 USER_GUIDE 6 與 Common situations。
