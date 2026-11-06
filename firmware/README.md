# 固件补丁（nRF_EPD 看板）

Windows 应用本身不需要改固件；这里的补丁只用于修复看板固件里阻塞刷新的缺陷。

## 为什么需要

`EPD/EPD_driver.c` 的 `EPD_ReadVoltage()` 有三处无超时的等待：

```c
while (!NRF_SAADC->EVENTS_STARTED);
while (!NRF_SAADC->EVENTS_END);
while (!NRF_SAADC->EVENTS_STOPPED);
```

这批板子上 SAADC 事件不来，函数永久卡死。`INIT (0x01)` 因此不返回，固件命令队列
`is_processing` 恒为真，后续 `REFRESH (0x05)` 永不执行。图像数据块 `0x31` 走紧急通道，
照常逐块 ACK，所以表面看"传输成功"，屏幕却一直空白。

`patches/0001-saadc-wait-timeout.patch` 给三处等待各加 20ms 超时，超时后沿用上一次有效读数
（初值 3300mV），不再阻塞命令队列。

注意：超时不能返回 0。固件把 `<=2800mV` 判为低电量、`<2700mV` 判为临界电量并直接进入关机
流程，返回 0 会让看板显示低电量警告页然后休眠。

## 构建

上游：`YCD12/nRF_EPD` @ `e86495e25fe69de1c7174b5913aa294c2103242e`（自带 SDK 17.1.0 目录）。

```powershell
# 便携工具链：xpack arm-none-eabi-gcc 10.3.1-2.3 + w64devkit 的 make
$env:PATH = "<gcc>\bin;<w64devkit>\bin;" + $env:PATH
$root = "<gcc>\bin\" -replace '\\','/'

git apply firmware/patches/0001-saadc-wait-timeout.patch
make -f Makefile.nRF52 "GNU_INSTALL_ROOT=$root"

tools\bin\nrfutil.exe pkg generate --application _build\nrf52811_xxaa.hex `
  --key-file tools\priv.pem --hw-version 52 --sd-req 0x126 --sd-id 0x126 `
  --application-version 0x22 _build\nrf52811_xxaa-0x22-ota.zip
```

注意两点：

- `Makefile.windows` 里写死了旧工具链路径，必须用 `GNU_INSTALL_ROOT` 覆盖。
- 若 `_build` 里有其它机器（WSL）留下的 `.d` 依赖文件，会报 `/mnt/c/...` 找不到源文件，
  需要把旧目录挪走再编。

## 烧录

只能走 BLE DFU（设备暴露 `0xFE59` 安全 DFU 服务）。属于设备写入，必须先取得用户明确同意。
写入前无固件备份能力，DFU 不支持回读。
