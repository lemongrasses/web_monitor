# 中文術語表（繁體中文，臺灣）

本表是 **web_monitor**（AIO System Dashboard，裝置上的儀表板）與 **aio-nav-receiver**（AIO NAV 接收監看，車端的桌面、iPhone 與 Android App）共用的中文術語，也是兩邊用字的共同基準：兩個專案的介面、文件與訊息都依本表用字，兩邊的測試也都讀這個檔案來檢查（見最後一節）。兩邊的檔案內容相同，修改時請同時更新兩個專案。

原則：

- 一律使用臺灣繁體中文（zh-TW）的工程用語；同一個意思，兩個專案用同一個中文詞；同一個中文詞，只用在一個意思。
- 縮寫保留英文：GNSS、IMU、INS、RTK、SPP、DSO、ZUPT、ZIHR、NHC、VUPT、ROS 2、LiDAR、UDP、TWD97。
- 先確認物理意義、參考框架與用途，再選詞；本表是首選用字，不是逐字對照表。中文依意思翻譯，不逐字對應英文。
- 文件引用介面文字時，照抄介面上的中文（例如「請讓車輛完全靜止。」）。
- 0x05 協定代碼（READY、KEEP_STILL_REQUIRED 等）的中文以下方「協定詞彙」為準：aio-nav-receiver 的 `src/aio_receiver/i18n.py`（iOS 與 Android 的測試再比對它）與儀表板操作手冊第 6.8 節的表格，都由測試對照本表。

## 工作流程對照

從上電到停止，同一個階段在兩邊的說法。對準分三個階段，每個階段有兩種名稱：**階段名稱**（燈號與進度，說明濾波器完成了什麼）與**動作**（操作人員該做什麼）。

| 階段 | 儀表板（產品頁面） | 0x05 狀態／要求的動作 | 車端接收程式（判斷：說明） |
|------|------|------|------|
| 上電、等待前置條件 | 未知 → 啟動中：「自動啟動中」「等待 …」 | 開機中 → 檢查中／讓車輛完全靜止 | 不可用：系統啟動中（路線「啟動」） |
| 啟動 AIO NAV | 啟動中：「正在啟動 AIO NAV」「等待第一筆導航輸出」 | AIO NAV 啟動中／讓車輛完全靜止 | 不可用：系統啟動中（路線「啟動」） |
| 對準第 1 階段：粗對準 | 初始化中：「對準第 1/3 階段：粗對準」；燈號「粗對準」；啟動進度「保持靜止」 | 保持靜止／讓車輛完全靜止 | 不可用：對準第 1 階段：保持靜止（路線「靜止」；燈號「粗對準」） |
| 對準第 2 階段：初始航向 | 初始化中：「對準第 2/3 階段：初始航向」；燈號「初始航向」；啟動進度「直線行駛」 | 直線行駛／在安全、空曠的區域以穩定速度直線行駛 | 不可用：對準第 2 階段：直線行駛（路線「直行」；燈號「初始航向」） |
| 對準第 3 階段：精對準 | 初始化中：「對準第 3/3 階段：精對準」；燈號「精對準」；啟動進度「精對準」 | 精對準／在有 GNSS 的環境下繼續正常行駛 | 不可用：對準第 3 階段：精對準（路線「精對準」；燈號「精對準」） |
| 就緒 | 就緒：「導航解可用」 | 就緒／不需動作 | 不可用：就緒確認中（需穩定 2 秒）→ 可用：導航輸出可以使用 |
| 就緒後失去必要條件 | 降級：「導航降級：…」 | 降級／停止使用導航，並依車輛安全程序處理 | 不可用：就緒後失去必要條件 |
| AIO NAV 或其輸出有問題 | 故障 | 故障／依情況 | 不可用：AIO NAV 故障 |
| 操作人員停止，或尚未啟動 | 已停止：「已由操作人員停止」，按鈕顯示「恢復」（未開啟自動啟動時顯示「啟動」） | 已停止／已停止，請在儀表板的總覽頁面按「恢復」或「啟動」 | 不可用：AIO NAV 已停止 |

「步驟」只用在儀表板的五個啟動步驟（感測器、GNSS／RTK、AIO NAV、對準、車端連線）；對準的三段一律稱「階段」。儀表板的狀態「啟動中」指 AIO NAV 正在啟動；0x05 的 BOOTING（儀表板剛開機）是「開機中」，兩者不混用。

## 導航狀態與判斷

| 英文 | 中文 | 說明 |
|------|------|------|
| Navigation | 導航 | 位置、速度、姿態的狀態估計與輸出；比「定位」範圍大 |
| Positioning, position fix | 定位 | 只指求出位置 |
| Navigation solution | 導航解 | 濾波器在某一時刻的輸出（位置、速度、姿態及其 σ） |
| Navigation output | 導航輸出 | 0x04 UDP 封包串流 |
| Navigation filter | 導航濾波器 | AIO NAV（IMU 與 GNSS 整合） |
| Inertial navigation | 慣性導航 | |
| Ready | 就緒 | 0x05 狀態 READY |
| Degraded | 降級 | 曾經就緒，之後失去必要條件或對準 |
| Fault | 故障 | |
| Stopped | 已停止 | 儀表板的已停止，也是 0x05 狀態 MAINTENANCE 的意思（操作人員停止，或尚未啟動） |
| Maintenance | 維護 | 只用於「需要維護」（SERVICE_REQUIRED）與維護頁面；維護頁面（Maintenance pages）只有英文，頁面名稱保留英文 |
| Usable / Blocked | 可用／不可用 | 車端接收程式（vehicle gate）對導航輸出的判斷；直接回答「導航輸出能不能用」 |
| Blocking condition (inhibit) | 不可用原因 | 0x05 的 inhibit_flags；任一位元為 1 即不可用 |
| Health condition | 成立條件 | 0x05 的 health_flags |
| Requested action | 要求的動作 | 0x05 的 requested_action |
| Ownership | 管理權 | AIO NAV 只能由一個管理者（儀表板的自動啟動，或 aio-nav.service）負責 |
| Operator | 操作人員 | |
| Vehicle side | 車端 | |

## 對準與輔助

| 英文 | 中文 | 說明 |
|------|------|------|
| Alignment | 對準 | 輸出可信之前，求出初始姿態與位置的整個過程（三個階段） |
| Coarse alignment (leveling) | 粗對準（調平） | 第 1 階段：靜止時由重力求出滾轉與俯仰；ALIGN 旗標 |
| Initial heading | 初始航向 | 第 2 階段：求出航向；HEADING 旗標（車端英文介面寫 Heading valid） |
| Fine alignment | 精對準 | 第 3 階段；FINE 旗標 |
| Alignment stage | 對準階段 | 「對準第 1 階段」「對準第 1/3 階段」；不稱「步驟」 |
| Aiding (update) | 輔助（更新） | |
| ZUPT | 零速更新（ZUPT） | 車輛靜止 |
| ZIHR | 零航向角速率更新（ZIHR） | 靜止時航向不變 |
| NHC | 非完整約束（NHC） | 車輛不側滑、不垂直跳動 |
| VUPT | 車速更新（VUPT） | 來自里程計 |
| Epoch, this epoch | 時刻、本時刻 | 一筆導航解的時間點 |

## 位置、運動與姿態

| 英文 | 中文 | 說明 |
|------|------|------|
| Position | 位置 | |
| Latitude / Longitude | 緯度／經度 | WGS84 |
| Height | 高度 | 0x04 沒有定義高度基準，所以不寫成橢球高或正高 |
| Ellipsoidal / orthometric height | 橢球高／正高 | 只在確定高度基準時使用 |
| Velocity | 速度 | 向量或其分量（北、東、上） |
| Speed, ground speed | 速率、水平速率 | 純量 |
| Vertical speed (up +) | 垂直速度（向上為正） | 帶正負號的分量 |
| Attitude | 姿態 | 滾轉、俯仰、航向 |
| Pose | 位姿 | 位置與姿態 |
| Roll / Pitch | 滾轉（角）／俯仰（角） | |
| Heading | 航向（角） | 0 = 北、90 = 東（順時針） |
| Yaw | 偏航（角） | 只用於 yaw 本身，例如安裝偏航角（mount_yaw）；不與航向混用 |
| North / East / Down / Up | 北／東／下／上 | 文件中也可寫北向、東向、地向、天向 |
| Angular rate | 角速率 | 例如靜止偵測的 Rotation |
| Track, trajectory | 軌跡 | |
| GPS time of week | GPS 週秒 | |
| Time stamp | 時間戳記 | |

## 準確度與不確定度

| 英文 | 中文 | 說明 |
|------|------|------|
| Accuracy | 準確度 | 與參考值（真值）的接近程度 |
| Precision | 精密度 | 重複量測的離散程度 |
| Uncertainty, σ (1-sigma), ± | 不確定度、1σ（標準差） | 濾波器或接收機**估計**的值，例如 0x04 的 σ 欄位、導航頁面的 ±、GNSS 的水平不確定度；不是實測誤差 |
| Error | 誤差 | 與真值之差；只在有參考值時使用 |
| Covariance | 共變異數 | |
| Centimetre / decimetre / metre level | 公分級／公寸級／公尺級 | |

「精度」一詞同時被當成準確度、精密度與不確定度使用，意思不明確，不使用；依意思改寫成上面三者之一。

## GNSS

| 英文 | 中文 | 說明 |
|------|------|------|
| GNSS receiver | GNSS 接收機 | |
| GNSS quality | GNSS 品質 | |
| RTK fixed / RTK float | RTK 固定解／RTK 浮動解 | |
| SPP, single point | 單點定位（SPP） | |
| No fix, No signal | 無定位 | 沒有可用的位置：0x05 的 GNSS 品質 1，也是儀表板 GNSS 燈的 No signal |
| Fix (type unknown) | 定位（類型不明） | |
| RTK corrections | RTK 修正資料 | 提供修正資料的網路服務是「修正服務」 |
| Open sky | 開闊天空、空曠處 | |

## 感測器與安裝

| 英文 | 中文 | 說明 |
|------|------|------|
| IMU | 慣性量測單元（IMU） | |
| Gyroscope / Accelerometer | 陀螺儀／加速度計 | |
| Bias / Scale factor | 偏差／比例因子 | |
| Lever arm | 桿臂 | GNSS 天線與後軸中心相對於 IMU 的位置 |
| Mounting angles (boresight) | 安裝角 | mount_pitch／mount_yaw：安裝俯仰角／安裝偏航角 |
| Calibration (parameter estimation) | 率定 | 例如外部參數率定、安裝角率定 |
| Correction, compensation | 校正、補償 | 對量測值或模型的調整 |
| Rear axle centre | 後軸中心 | AIO NAV 的車輛參考點 |
| Odometer / Visual odometry | 里程計／視覺里程計（DSO） | |
| LiDAR | LiDAR（光達） | 介面寫 LiDAR；文件第一次出現時寫「光達（LiDAR）」 |
| Point cloud | 點雲 | |
| Camera | 相機 | |
| Still, still detection | 靜止、靜止偵測 | |

## 封包、通訊與系統

| 英文 | 中文 | 說明 |
|------|------|------|
| Packet | 封包 | |
| Navigation packet (0x04) | 導航封包（0x04） | |
| Status packet (0x05) | 狀態封包（0x05） | 儀表板的使用者頁面稱「就緒訊號」（Ready signal） |
| Port, port number | 連接埠、埠號 | |
| Sender | 發送者、發送端 | |
| Session / Sequence | 工作階段／序號 | |
| Age (of data) | 延遲 | 現在與最近一筆資料的時間差，例如「0x04 延遲 (ms)」 |
| Stale | 延遲 | 超過預期時間沒有新資料；更久則為「中斷」 |
| Lost | 中斷 | |
| Timeout | 逾時 | |
| Rate | 頻率 | |
| Uptime | 運作時間 | |
| Booting / Starting | 開機中／啟動中 | 開機中：儀表板剛開機；啟動中：AIO NAV 正在啟動 |
| Restart | 重新啟動 | |
| Resume | 恢復 | 操作人員停止後，總覽頁面的按鈕 |
| Replay | 重播 | |
| Dashboard | 儀表板 | |
| Monitor, monitoring | 監看 | 例如「AIO NAV 接收監看」只接收、不送回任何東西；會自動重新啟動程式的 watchdog 也可寫「監控」 |
| Hotspot | 熱點 | |
| Coordinate system, TWD97 | 座標系統、TWD97 | |
| Inbound (firewall) | 輸入 | Windows 防火牆的輸入規則 |

## 協定詞彙

車端接收程式把 0x05 的代碼顯示成下列文字（「English（車端）」是車端英文介面的文字）。儀表板操作手冊第 6.8 節的表格用同樣的中文。

### 狀態（state）

| 代碼 | 協定名稱 | 中文 | English（車端） | 意義 |
|---:|---|---|---|---|
| 0 | `BOOTING` | 開機中 | Booting | 儀表板剛開機 |
| 1 | `CHECKING` | 檢查中 | Checking | 等待前置條件或自動啟動 |
| 2 | `NAV_STARTING` | AIO NAV 啟動中 | Starting AIO NAV | AIO NAV 已啟動，尚無有效輸出 |
| 3 | `KEEP_STILL` | 保持靜止 | Keep still | 對準第 1 階段（粗對準） |
| 4 | `MOVE_STRAIGHT` | 直線行駛 | Drive straight | 對準第 2 階段（初始航向） |
| 5 | `REFINING` | 精對準 | Refining | 對準第 3 階段，或已對準但缺少必要條件 |
| 6 | `READY` | 就緒 | Ready | 可以使用 |
| 7 | `DEGRADED` | 降級 | Degraded | 曾經就緒，之後失去必要條件或對準 |
| 8 | `FAULT` | 故障 | Fault | AIO NAV、其輸出或管理權有問題 |
| 9 | `MAINTENANCE` | 已停止 | Stopped | 操作人員停止，或尚未啟動 |

### 要求的動作（requested_action）

| 代碼 | 協定名稱 | 車端顯示 | 儀表板顯示 | English（車端） |
|---:|---|---|---|---|
| 0 | `NONE` | 不需動作 | （不顯示） | Nothing to do |
| 1 | `HOLD_STILL` | 讓車輛完全靜止 | 請讓車輛完全靜止。 | Keep the vehicle completely still |
| 2 | `DRIVE_STRAIGHT` | 在安全、空曠的區域以穩定速度直線行駛 | 請在安全、空曠的區域以穩定速度直線行駛。 | Drive straight at a steady speed in a safe, open area |
| 3 | `KEEP_DRIVING` | 在有 GNSS 的環境下繼續正常行駛 | 請在有 GNSS 的環境下繼續正常行駛。 | Keep driving normally where GNSS is available |
| 4 | `SAFE_STOP` | 停止使用導航，並依車輛安全程序處理 | 請停止使用導航，並依車輛安全程序處理。 | Stop using navigation and follow the vehicle's safety procedure |
| 5 | `SERVICE_REQUIRED` | 需要維護 | 需要維護。 | Maintenance is needed |
| 6 | `MAINTENANCE_HOLD` | 已停止，請在儀表板的總覽頁面按「恢復」或「啟動」 | 已停止。請在總覽頁面按「恢復」或「啟動」。 | Stopped. Press Resume or Start on the dashboard's Overview page |

### GNSS 品質（gnss_quality）

| 代碼 | 中文 | English（車端） |
|---:|---|---|
| 0 | 未知 | Unknown |
| 1 | 無定位 | No fix |
| 2 | 單點定位 | Single point |
| 3 | RTK 浮動解 | RTK float |
| 4 | RTK 固定解 | RTK fixed |

### 成立條件（health_flags）

| 位元 | 協定名稱 | 中文 | English（車端） |
|---:|---|---|---|
| 0 | `DASHBOARD_OK` | 儀表板正常 | Dashboard OK |
| 1 | `CONFIG_OK` | 設定有效 | Configuration valid |
| 2 | `IMU_STREAM_OK` | IMU 資料正常 | IMU data OK |
| 3 | `GNSS_STREAM_OK` | GNSS 資料正常 | GNSS data OK |
| 4 | `GNSS_QUALITY_OK` | GNSS 品質足夠 | GNSS quality sufficient |
| 5 | `NAV_PROCESS_OK` | AIO NAV 執行中 | Navigation process running |
| 6 | `NAV_OUTPUT_OK` | 導航輸出正常 | Navigation output OK |
| 7 | `ALIGNMENT_OK` | 粗對準完成 | Coarse alignment done |
| 8 | `HEADING_OK` | 初始航向完成 | Heading valid |
| 9 | `FINE_ALIGNMENT_OK` | 精對準完成 | Fine alignment done |
| 10 | `DSO_OK` | DSO 正常 | DSO OK |
| 11 | `EXTERNAL_ROUTE_OK` | 到車端的路由正常 | Route to the vehicle OK |
| 12 | `RTK_CORRECTION_OK` | RTK 修正資料正常 | RTK corrections OK |
| 13 | `STORAGE_OK` | 儲存空間可寫入 | Storage writable |
| 14 | `VEHICLE_ACK_OK` | 車端已確認接收 | Vehicle acknowledged |
| 15 | `TIME_SYNC_OK` | 時間同步正常 | Time synchronised |
| 16 | `NO_DUPLICATE_PROCESS` | AIO NAV 沒有重複執行 | No duplicate process |
| 17 | `WIFI_OK` | Wi-Fi 正常 | Wi-Fi OK |

### 不可用原因（inhibit_flags）

| 位元 | 協定名稱 | 中文 | English（車端） |
|---:|---|---|---|
| 0 | `BOOT_IN_PROGRESS` | 開機中 | Booting |
| 1 | `CONFIG_INVALID` | 設定無效 | Configuration invalid |
| 2 | `IMU_MISSING_OR_STALE` | IMU 資料缺少或延遲 | IMU data missing or stale |
| 3 | `GNSS_MISSING_OR_STALE` | GNSS 資料缺少或延遲 | GNSS data missing or stale |
| 4 | `GNSS_QUALITY_LOW` | GNSS 品質不足 | GNSS quality too low |
| 5 | `NAV_NOT_RUNNING` | AIO NAV 未執行 | Navigation not running |
| 6 | `NAV_OUTPUT_STALE` | 導航輸出延遲 | Navigation output stale |
| 7 | `KEEP_STILL_REQUIRED` | 需要保持靜止 | Must keep still |
| 8 | `MOVE_STRAIGHT_REQUIRED` | 需要直線行駛 | Must drive straight |
| 9 | `FINE_ALIGNMENT_REQUIRED` | 需要精對準 | Fine alignment needed |
| 10 | `DSO_NOT_READY` | DSO 尚未就緒 | DSO not ready |
| 11 | `EXTERNAL_ROUTE_DOWN` | 沒有到車端的路由 | No route to the vehicle |
| 12 | `RTK_CORRECTION_LOST` | RTK 修正資料中斷 | RTK corrections lost |
| 13 | `STORAGE_NOT_WRITABLE` | 儲存空間無法寫入 | Storage not writable |
| 14 | `MAINTENANCE_HOLD` | 操作人員已停止 | Stopped by an operator |
| 15 | `STARTUP_TIMEOUT` | 啟動逾時 | Startup timed out |
| 16 | `OWNERSHIP_CONFLICT` | 管理權衝突 | Ownership conflict |
| 17 | `INTERNAL_FAULT` | 內部錯誤 | Internal fault |

### 對準旗標（0x04 flags）

| 旗標 | 中文 | English（車端） | 儀表板英文介面 |
|---|---|---|---|
| `ALIGN` | 粗對準 | Coarse aligned | Coarse alignment |
| `HEADING` | 初始航向 | Heading valid | Initial heading |
| `FINE` | 精對準 | Fine aligned | Fine alignment |

## 不使用的詞

兩個專案的測試都讀這張表：介面文字與 zh-TW 手冊中出現左欄的詞就算失敗（左欄的詞是右欄某個詞的一部分時不算，例如「時間戳記」）。

| 不使用 | 改用 | 原因 |
|---|---|---|
| 橫滾 | 滾轉 | 臺灣航太與導航用語 |
| 浮點解 | 浮動解 | 臺灣測量用語 |
| 槓桿臂 | 桿臂 | |
| 接收器 | 接收機 | |
| 校正資料 | 修正資料 | RTK 的 corrections；「校正」保留給量測值的調整 |
| 坐標 | 座標 | |
| 分米 | 公寸 | |
| 週內秒 | 週秒 | |
| 時間戳 | 時間戳記 | |
| 回放 | 重播 | |
| 重啟 | 重新啟動 | |
| 操作員 | 操作人員 | |
| 入站 | 輸入 | Windows 防火牆的用語 |
| 運行 | 運作、執行 | |
| 年齡 | 延遲 | 資料的 age |
| 過舊 | 延遲 | stale |
| 精度 | 準確度、精密度、不確定度 | 意思不明確 |
| 阻擋 | 不可用 | 沒說出什麼被擋、對操作人員代表什麼 |
| 擁有權 | 管理權 | |
| 維護暫停 | 操作人員已停止 | MAINTENANCE_HOLD 是操作人員停止，不是需要維護 |
| 對外路由 | 到車端的路由 | |
| 無訊號 | 無定位 | 有訊號也可能沒有可用的位置 |
