# 中文術語表（繁體中文，臺灣）

本表是 **web_monitor**（AIO System Dashboard，裝置上的儀表板）與 **aio-nav-receiver**（AIO NAV 接收監看，車端的桌面、iPhone 與 Android App）共用的中文術語。兩個專案的介面、文件與訊息都依本表用字；兩邊的檔案內容相同，修改時請同時更新兩個專案。

原則：

- 一律使用臺灣繁體中文（zh-TW）的工程用語；同一個英文詞、同一個意思，兩個專案用同一個中文詞。
- 縮寫保留英文：GNSS、IMU、INS、RTK、SPP、DSO、ZUPT、ZIHR、NHC、VUPT、ROS 2、LiDAR、UDP、TWD97。
- 先確認物理意義、參考框架與用途，再選詞；本表是首選用字，不是逐字對照表。
- 文件引用介面文字時，照抄介面上的中文（例如「請讓車輛完全靜止。」）。
- 協定代碼（READY、KEEP_STILL_REQUIRED 等）的中文以 aio-nav-receiver 的 `src/aio_receiver/i18n.py` 為準（iOS 與 Android 的測試會比對它），儀表板文件第 6.8 節的表格與它一致。

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
| Maintenance | 維護 | 0x05 狀態 MAINTENANCE；維護頁面（Maintenance pages）只有英文，頁面名稱保留英文 |
| Usable / Blocked | 可用／阻擋 | 車端接收程式（vehicle gate）對導航輸出的判斷 |
| Blocking condition (inhibit) | 阻擋條件 | 0x05 的 inhibit_flags；任一位元為 1 即阻擋 |
| Health condition | 成立條件 | 0x05 的 health_flags |
| Requested action | 要求的動作 | 0x05 的 requested_action |
| Operator | 操作人員 | 不用「操作員」 |
| Vehicle side | 車端 | |

## 對準與輔助

| 英文 | 中文 | 說明 |
|------|------|------|
| Alignment | 對準 | 輸出可信之前，求出初始姿態與位置的過程 |
| Coarse alignment (leveling) | 粗對準（調平） | 靜止時由重力求出滾轉與俯仰 |
| Initial heading, Heading valid | 初始航向、航向有效 | HEADING 旗標；儀表板英文介面寫 Initial heading，接收程式寫 Heading valid |
| Fine alignment | 精對準 | |
| Aiding (update) | 輔助（更新） | |
| ZUPT | 零速更新（ZUPT） | 車輛靜止 |
| ZIHR | 零航向角速率更新（ZIHR） | 靜止時航向不變 |
| NHC | 非完整約束（NHC） | 車輛不側滑、不垂直跳動 |
| VUPT | 車速更新（VUPT） | 來自里程計 |
| Epoch, this epoch | 時刻、本時刻 | 一筆導航解的時間點；不用「本次」 |

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
| Roll / Pitch | 滾轉（角）／俯仰（角） | 不用「橫滾」 |
| Heading | 航向（角） | 0 = 北、90 = 東（順時針） |
| Yaw | 偏航（角） | 只用於 yaw 本身，例如安裝偏航角（mount_yaw）；不與航向混用 |
| North / East / Down / Up | 北／東／下／上 | 文件中也可寫北向、東向、地向、天向 |
| Angular rate | 角速率 | 例如靜止偵測的 Rotation |
| Track, trajectory | 軌跡 | |
| GPS time of week | GPS 週秒 | 不用「週內秒」 |
| Time stamp | 時間戳記 | |

## 準確度與不確定度

| 英文 | 中文 | 說明 |
|------|------|------|
| Accuracy | 準確度 | 與參考值（真值）的接近程度 |
| Precision | 精密度 | 重複量測的離散程度 |
| Uncertainty, σ (1-sigma), ± | 不確定度、1σ（標準差） | 濾波器或接收機**估計**的值，例如 0x04 的 σ 欄位、導航頁面的 ±、GNSS 的水平不確定度；不是實測誤差 |
| Error | 誤差 | 與真值之差；只在有參考值時使用 |
| Covariance | 共變異數 | |
| Centimetre / decimetre / metre level | 公分級／公寸級／公尺級 | 不用「分米」 |

「精度」一詞同時被當成準確度、精密度與不確定度使用，意思不明確，不使用；依意思改寫成上面三者之一。

## GNSS

| 英文 | 中文 | 說明 |
|------|------|------|
| GNSS receiver | GNSS 接收機 | 不用「接收器」 |
| GNSS quality | GNSS 品質 | |
| RTK fixed / RTK float | RTK 固定解／RTK 浮動解 | 不用「浮點解」 |
| SPP, single point | 單點定位（SPP） | |
| No fix | 無定位 | 0x05 的 GNSS 品質 1 |
| No signal | 無訊號 | 儀表板的 GNSS 燈 |
| Fix (type unknown) | 定位（類型不明） | |
| RTK corrections | RTK 修正資料 | 不用「校正資料」；提供修正資料的網路服務是「修正服務」 |
| Open sky | 開闊天空、空曠處 | |

## 感測器與安裝

| 英文 | 中文 | 說明 |
|------|------|------|
| IMU | 慣性量測單元（IMU） | |
| Gyroscope / Accelerometer | 陀螺儀／加速度計 | |
| Bias / Scale factor | 偏差／比例因子 | |
| Lever arm | 桿臂 | GNSS 天線與後軸中心相對於 IMU 的位置；不用「槓桿臂」 |
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
| Restart | 重新啟動 | 不用「重啟」 |
| Replay | 重播 | 不用「回放」 |
| Dashboard | 儀表板 | |
| Monitor, monitoring | 監看 | 例如「AIO NAV 接收監看」只接收、不送回任何東西；會自動重新啟動程式的 watchdog 也可寫「監控」 |
| Hotspot | 熱點 | |
| Coordinate system, TWD97 | 座標系統、TWD97 | 不用「坐標」 |
| Inbound (firewall) | 輸入 | 不用「入站」 |
