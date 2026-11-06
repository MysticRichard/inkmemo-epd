# 第三方组件与声明

本项目以 GPL-3.0 发布。以下组件由本项目直接依赖或在构建产物中出现，版权归各自作者所有。

## 上游项目

| 项目 | 用途 | 许可 |
| --- | --- | --- |
| [YCD12/nRF_EPD](https://github.com/YCD12/nRF_EPD) `e86495e` | BLE 协议与固件基线；`firmware/patches/` 为其衍生补丁 | GPL-3.0 |

`firmware/` 仅包含补丁与说明，不包含 Nordic nRF5 SDK 源码。若自行编译固件，需另行从 Nordic 获取 SDK 17.1.0 并遵守其许可条款。

## Python 依赖

| 组件 | 用途 | 许可 |
| --- | --- | --- |
| bleak | BLE 通信 | MIT |
| playwright | 无头读取 WPS 页面 | Apache-2.0 |
| openpyxl | 表格解析辅助 | MIT |
| Pillow | 图像与位平面处理 | MIT-CMU (HPND) |
| pystray | 系统托盘 | LGPL-3.0 |
| lunardate | 农历换算 | MIT |
| winrt-Windows.Devices.Geolocation | Windows 定位回退 | MIT |
| pytest | 测试 | MIT |
| PyInstaller | 打包 | GPL-2.0-or-later，含打包例外条款 |

## 字体

渲染只使用 Windows 系统自带字体（微软雅黑、宋体），**仓库不分发任何字体文件**。字体授权随 Windows 系统许可，不因本项目而转移。

## 数据来源

- 天气与城市反查：[Open-Meteo](https://open-meteo.com/) 与 BigDataCloud 公开接口，按其使用条款调用。
