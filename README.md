# 墨笺 InkMemo

Windows 后台读取指定 WPS 共享表格，将五字段事项排版为 800×480 黑白红画面，经本机 BLE 发送到 nRF_EPD 墨水屏。

## 当前状态

需求与三套 UI 视觉已批准。应用已具备 WPS 云端读取、五字段解析排序、三套批准版式、800×480 位平面、BLE 0x31 传输、Windows 托盘与设置入口。标准 v0.11、宽松 v0.16 与紧凑 v0.19 已完成视觉验收，三套版式均已打包。

2026-11-05 已完成一次 WPS 桌面进程关闭后的真实闭环：后台读取 9 项、自动选择标准版、BLE 双层确认并刷新，用户目视确认成功。随后真实 `1800` 秒检查在 `12:33:18` 读到 4 项并自动切换宽松版，但目标 `NRF_EPD_F3FA` 虽在广播，GATT 服务发现连续超时，屏幕保留上一张完整画面；用户已确认收到“蓝牙同步失败”通知。剩余验收：看板复位后的 4 项宽松版上屏、登录自启、暂停/恢复、WPS 链接更换和云端失败缓存提示。

- 标准 UI：`design/approved/standard-v0.11/`（结构沿用已批准 v0.6，逾期图标为 v0.11）
- 宽松 UI：`design/approved/relaxed-v0.16/`（1–5 项）
- 标准 UI：`design/approved/standard-v0.11/`（6–12 项；11–12 项使用标准容量子样式）
- 紧凑 UI：`design/approved/compact-v0.15/`（13–30 项；运行时为 v0.19 三列左对齐实现）
- 当前 PRD：`docs/PRD-v0.3.md`
- 开发指南：`docs/development-guide-v0.3.md`
- 当前基线：`docs/current-baseline.md`
- 使用说明：`docs/user-guide.md`

Obsidian 项目中的 `当前基线.md` 是需求与批准记录的权威入口；仓库文档用于开发协作与版本追踪。

## 目标平台

- Windows 11 x64
- 800×480 黑白红 UC8179 / nRF_EPD 兼容屏幕
- WPS 共享表格，`A:E` 五字段
- 本机 BLE，无额外网关或云服务器

## 开发运行

```powershell
.\.venv\Scripts\python.exe -m inkmemo preview
.\.venv\Scripts\python.exe -m inkmemo scan
.\.venv\Scripts\python.exe -m inkmemo calibrate
.\.venv\Scripts\python.exe -m inkmemo login
.\.venv\Scripts\python.exe -m inkmemo run
```

预览与运行状态保存在 `%LOCALAPPDATA%\InkMemo\`，不会写入仓库。

与 Magpie LCD Bridge 共用本机蓝牙时，墨笺会在扫描、校准和传图期间申请 LCD 租约；具体行为见 `docs/user-guide.md`。

2026-11-05 已部署租约版本并完成 3 次校准验证；异常结束校准进程后，LCD 能在默认 120 秒 TTL 到期后自动恢复。实体屏最终画面仍需现场目视确认。

## 目录版打包

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\scripts\build.ps1
```

产物为 `dist\InkMemo\InkMemo.exe`。

## 安全

真实 WPS 链接、事项原文、姓名、登录会话、Token、Cookie 和含原文截图不得提交到本仓库。

## 上游

- [YCD12/nRF_EPD](https://github.com/YCD12/nRF_EPD)
- 协议基线提交：`e86495e25fe69de1c7174b5913aa294c2103242e`
