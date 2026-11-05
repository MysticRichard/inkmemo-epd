from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from ..models import LayoutResult
from .lease import BleLeaseBusy, acquire_ble_lease
from .protocol import (
    CMD_CLEAR,
    CMD_QUERY_STATUS,
    CMD_REFRESH,
    CMD_RESET_TRANSFER,
    CMD_SET_MODE,
    COMMAND_UUID,
    LAYER_BLACK,
    LAYER_RED,
    MODE_CLOCK,
    PLANE_BYTES,
    SERVICE_UUID,
    VERSION_UUID,
    block_count,
    build_block,
    parse_ack,
    parse_transfer_status,
)


class BleTransferError(RuntimeError):
    pass


_log = logging.getLogger("inkmemo.ble")

# WinRT 的 GATT 连接、服务发现和写入都不自带超时；卡住时必须由我们兜底，
# 否则进程会一直挂在半开链路上，只能重启电脑蓝牙才能恢复。
SCAN_SLACK = 5.0
DISCONNECT_BUDGET = 8.0
WRITE_TIMEOUT = 15.0


class DeviceNotFound(BleTransferError):
    pass


class BleConnectTimeout(BleTransferError):
    pass


class LowBattery(BleTransferError):
    pass


class TransferAborted(RuntimeError):
    pass


@dataclass(frozen=True)
class EpdConfig:
    model_id: int
    wakeup_pin: int
    display_mode: int


def parse_config(payload: bytes) -> EpdConfig | None:
    # The firmware sends its 14-byte epd_config_t notification right after the
    # CCCD write; field offsets come from EPD/EPD_config.h.
    if len(payload) != 14:
        return None
    return EpdConfig(model_id=payload[7], wakeup_pin=payload[8], display_mode=payload[11])


@dataclass(frozen=True)
class EpdInfo:
    version: int
    mtu: int
    voltage_mv: int | None
    config: EpdConfig | None = None


@dataclass(frozen=True)
class TransferResult:
    info: EpdInfo
    layout: LayoutResult
    refresh_requested: bool
    refresh_uncertain: bool


class EpdConnection:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.command = None
        self.version_char = None
        self.payload_size = 20
        self.info: EpdInfo | None = None
        self.config: EpdConfig | None = None
        self._notifications: asyncio.Queue[bytes] = asyncio.Queue()
        self._init_lock = asyncio.Lock()
        self.disconnected = asyncio.Event()

    @classmethod
    async def connect(cls, name: str, timeout: float = 15.0) -> EpdConnection:
        try:
            device = await asyncio.wait_for(
                BleakScanner.find_device_by_name(name, timeout=timeout),
                timeout=timeout + SCAN_SLACK,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"扫描 {name} 超时") from exc
        if device is None:
            raise DeviceNotFound(f"未发现目标设备 {name}")
        connection: EpdConnection | None = None

        def on_disconnect(_client: BleakClient) -> None:
            if connection is not None:
                connection.disconnected.set()

        client = BleakClient(
            device,
            disconnected_callback=on_disconnect,
            timeout=timeout,
            # A stale Windows GATT cache can make service discovery hang even
            # though the device is advertising; always discover fresh.
            winrt={"use_cached_services": False},
        )
        connection = cls(client)
        try:
            await asyncio.wait_for(client.connect(), timeout=timeout)
        except BaseException:
            # Windows keeps a half-open link if the failed connect is not torn
            # down; the next attempt then times out too.
            await _release(client)
            raise
        try:
            await asyncio.wait_for(connection._bind(), timeout=timeout)
        except BaseException:
            await _release(client)
            raise
        return connection

    async def _bind(self) -> None:
        services = self.client.services
        self.command = services.get_characteristic(COMMAND_UUID)
        self.version_char = services.get_characteristic(VERSION_UUID)
        service = services.get_service(SERVICE_UUID)
        if self.command is None or self.version_char is None or service is None:
            raise BleTransferError("目标设备缺少 nRF_EPD 服务或特征")
        max_payload = int(getattr(self.command, "max_write_without_response_size", 20) or 20)
        self.payload_size = min(max_payload - 8, 248)
        if self.payload_size < 20:
            raise BleTransferError("BLE 写入负载过小，无法使用 0x31 协议")
        await self.client.start_notify(COMMAND_UUID, self._on_notify)

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        payload = bytes(data)
        if self.config is None:
            self.config = parse_config(payload)
        self._notifications.put_nowait(payload)

    async def initialize(self) -> EpdInfo:
        version_raw = await self.client.read_gatt_char(VERSION_UUID)
        version = version_raw[0] if version_raw else 0
        if version < 0x20:
            raise BleTransferError(f"目标固件 0x{version:02x} 不支持 0x31 CRC 分块协议")
        async with self._init_lock:
            # 不发 INIT：这块板子的固件在 INIT 里读电压时没有超时，会卡死命令队列。
            # 面板的复位与 PSR/CDI 由后续 SET_MODE(CLOCK) -> UC8179_Init() 完成；
            # 不能发送 INIT(0x01)，因为目标 0x20 固件会卡在电压读取。
            if self.config is not None:
                _log.info(
                    "device config model=%d wakeup_pin=0x%02x display_mode=%d",
                    self.config.model_id,
                    self.config.wakeup_pin,
                    self.config.display_mode,
                )
            self.info = EpdInfo(
                version=version,
                mtu=self.payload_size,
                voltage_mv=None,
                config=self.config,
            )
            return self.info

    async def query_transfer_status(self, timeout: float = 2.0) -> dict | None:
        self._drain()
        try:
            await self._write(bytes([CMD_QUERY_STATUS]))
        except Exception:
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                payload = await asyncio.wait_for(
                    self._notifications.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return None
            status = parse_transfer_status(payload)
            if status is not None:
                total, received, active = status
                return {"total": total, "received": received, "active": active}

    async def send_frame(
        self,
        frame: LayoutResult,
        refresh_wait: float = 35.0,
        # 45 秒实测会出现时钟页与新画面叠图，说明时钟重绘尚未结束就写入；
        # 留到 75 秒后再写块，代价是每次同步多等 30 秒。
        panel_reset_wait: float = 75.0,
        # 清屏要跑完整全刷（最多 30 秒），等它结束再写块。
        panel_clear_wait: float = 40.0,
        should_abort: Callable[[], bool] | None = None,
    ) -> tuple[bool, bool]:
        if self.info is None:
            raise BleTransferError("发送前未完成设备配置")
        if len(frame.black) != PLANE_BYTES or len(frame.red) != PLANE_BYTES:
            raise BleTransferError("位平面长度不是 48000 字节")
        if should_abort and should_abort():
            raise TransferAborted("传输已取消")
        await self._reset_panel(panel_reset_wait, panel_clear_wait, should_abort)
        await self._send_layer(LAYER_BLACK, frame.black, should_abort)
        black_status = await self.query_transfer_status()
        if black_status is None or black_status["received"] < black_status["total"]:
            raise BleTransferError("黑白层状态确认不完整")
        await self._send_layer(LAYER_RED, frame.red, should_abort)
        red_status = await self.query_transfer_status()
        if red_status is None or red_status["received"] < red_status["total"]:
            raise BleTransferError("红色层状态确认不完整")
        if should_abort and should_abort():
            raise TransferAborted("传输已取消")
        # The firmware has no explicit refresh-complete notification.  A
        # disconnect during refresh is not proof that the panel finished.
        uncertain = True
        refresh_started = time.monotonic()
        try:
            # 刷新命令没有设备回执，必须用带响应写入确认固件已收到；
            # 无响应写入在缓冲繁忙时可能被静默丢弃，屏幕会停留在旧画面。
            await self._write(bytes([CMD_REFRESH]), response=True)
        except Exception as exc:
            raise BleTransferError("刷新命令写入失败") from exc
        # 固件把刷新命令放入异步队列，写入成功不等于已执行。
        _log.info(
            "layers confirmed black=%d/%d red=%d/%d, refresh queued",
            black_status["received"],
            black_status["total"],
            red_status["received"],
            red_status["total"],
        )
        # UC8179_Refresh blocks the firmware main loop for up to 30s, but 0x32
        # is handled inline.  Probing keeps the link busy and shows whether the
        # board is still executing the refresh or already went to sleep.
        probes = 0
        while not self.disconnected.is_set():
            remaining = refresh_wait - (time.monotonic() - refresh_started)
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self.disconnected.wait(), timeout=min(2.0, remaining))
                break
            except asyncio.TimeoutError:
                pass
            try:
                await self._write(bytes([CMD_QUERY_STATUS]))
                probes += 1
            except Exception:
                break
        elapsed = time.monotonic() - refresh_started
        if self.disconnected.is_set():
            # The firmware sleeps the panel on disconnect unless its own GUI
            # path is mid-refresh, so a drop here can abort the physical refresh
            # and leave the previous image on screen.
            _log.warning("link dropped %.1fs into refresh window (probes=%d)", elapsed, probes)
        else:
            _log.info("refresh window held %.1fs (probes=%d)", elapsed, probes)
        # UC8179_Refresh blocks up to 30s.  Do not let a caller start another
        # transfer while the panel may still be refreshing, even if BLE dropped.
        remaining = refresh_wait - (time.monotonic() - refresh_started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        return True, uncertain

    async def _reset_panel(
        self,
        wait: float,
        clear_wait: float = 0.0,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        # 断开连接后固件会把面板打进 DSLP，睡眠中的面板忽略所有 SPI 写入，
        # 刷新因此无效。切到时钟模式会让固件在写第一块前执行 UC8179_Init
        # （硬件复位 + PSR/CDI），这是不依赖 INIT(0x01) 的唯一复位路径。
        if wait <= 0:
            return
        try:
            await self._write(bytes([CMD_SET_MODE, MODE_CLOCK]), response=True)
        except Exception as exc:
            raise BleTransferError("面板复位命令写入失败") from exc
        # 固件切换模式后会重绘时钟页，需要等它画完再写图，否则会被覆盖。
        deadline = time.monotonic() + wait
        while True:
            if should_abort and should_abort():
                raise TransferAborted("传输已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(2.0, remaining))
        if clear_wait <= 0:
            return
        # 时钟模式用局刷 + 外部 LUT，直接写图会让刷新退化成局刷，
        # 屏幕出现“新画面很淡 + 旧时钟/日历残影”。清屏(0x02)会走完整
        # UC8179_Refresh 并回到 OTP LUT，先把面板刷成纯白再写图。
        try:
            await self._write(bytes([CMD_CLEAR]), response=True)
        except Exception as exc:
            raise BleTransferError("清屏命令写入失败") from exc
        deadline = time.monotonic() + clear_wait
        while True:
            if should_abort and should_abort():
                raise TransferAborted("传输已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(2.0, remaining))

    async def _send_layer(
        self,
        layer: int,
        data: bytes,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        if should_abort and should_abort():
            raise TransferAborted("传输已取消")
        self._drain()
        await self._write(bytes([CMD_RESET_TRANSFER]))
        await asyncio.sleep(0.1)
        total = block_count(len(data), self.payload_size)
        if total > 512:
            raise BleTransferError("分块数超过 512")
        for block_id in range(total):
            if should_abort and should_abort():
                raise TransferAborted("传输已取消")
            offset = block_id * self.payload_size
            payload = data[offset : offset + self.payload_size]
            packet = build_block(block_id, total, layer, payload)
            try:
                await self._write(packet)
                ack_id, status = await self._wait_ack(block_id, should_abort=should_abort)
            except BleTransferError:
                raise
            except TransferAborted:
                raise
            except Exception as exc:
                raise BleTransferError(f"第 {block_id} 块写入中断") from exc
            if ack_id != block_id:
                raise BleTransferError(f"ACK 块号不匹配：期望 {block_id}，收到 {ack_id}")
            if status != 0:
                raise BleTransferError(f"第 {block_id} 块返回 NACK {status}")

    async def _wait_ack(
        self,
        block_id: int,
        timeout: float = 4.0,
        should_abort: Callable[[], bool] | None = None,
    ) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        while True:
            if should_abort and should_abort():
                raise TransferAborted("传输已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BleTransferError(f"等待第 {block_id} 块 ACK 超时")
            try:
                data = await asyncio.wait_for(
                    self._notifications.get(), timeout=min(remaining, 0.5)
                )
            except asyncio.TimeoutError:
                continue
            ack = parse_ack(data)
            if ack is not None:
                return ack

    def _drain(self) -> None:
        while not self._notifications.empty():
            self._notifications.get_nowait()

    async def _write(
        self, payload: bytes, *, response: bool = False, timeout: float = WRITE_TIMEOUT
    ) -> None:
        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(COMMAND_UUID, payload, response=response),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise BleTransferError("BLE 写入超时") from exc

    async def close(self) -> None:
        await _release(self.client)


async def _release(client: BleakClient) -> None:
    """Tear down the Windows GATT link so the next connect does not hang."""
    backend = getattr(client, "_backend", None)
    session = getattr(backend, "_session", None)
    if session is not None:
        # Dropping this first stops Windows from keeping the ACL link alive
        # after our process closes the session.
        try:
            session.maintain_connection = False
        except Exception:
            pass
    try:
        await asyncio.wait_for(asyncio.shield(client.disconnect()), timeout=DISCONNECT_BUDGET)
    except BaseException:
        pass
    _release_winrt_client(client)


def _release_winrt_client(client: BleakClient) -> None:
    """Close the WinRT GATT session that Bleak may leave half-open on timeout."""
    backend = getattr(client, "_backend", None)
    session = getattr(backend, "_session", None)
    if session is not None:
        try:
            session.maintain_connection = False
        except Exception:
            pass
        try:
            session.close()
        except Exception:
            pass
    requester = getattr(backend, "_requester", None)
    if requester is not None:
        try:
            requester.close()
        except Exception:
            pass


FrameFactory = Callable[[EpdInfo], Awaitable[LayoutResult]]

# 实测：连接成功只需 7–14 秒，失败会一直挂到超时；同一次失败后马上重试常常
# 立刻成功。所以用短预算多试几次，比长时间死等更可靠。
RETRY_DELAYS = (5.0, 10.0, 15.0, 20.0)


async def transfer_with_retries(
    device_name: str,
    frame_factory: FrameFactory,
    *,
    attempts: int = 5,
    connect_timeout: float = 30.0,
    should_abort: Callable[[], bool] | None = None,
) -> TransferResult:
    async with acquire_ble_lease("inkmemo") as lease_session:

        def combined_abort() -> bool:
            return lease_session.should_abort() or bool(
                should_abort and should_abort()
            )

        try:
            return await _transfer_with_retries_body(
                device_name,
                frame_factory,
                attempts=attempts,
                connect_timeout=connect_timeout,
                should_abort=combined_abort,
            )
        except TransferAborted:
            if lease_session.should_abort():
                raise BleLeaseBusy("LCD 蓝牙租约已失效") from None
            raise


async def _transfer_with_retries_body(
    device_name: str,
    frame_factory: FrameFactory,
    *,
    attempts: int = 5,
    connect_timeout: float = 30.0,
    should_abort: Callable[[], bool] | None = None,
) -> TransferResult:
    last_error: Exception | None = None
    last_error_was_connect = False
    for attempt in range(attempts):
        started = time.monotonic()
        if should_abort and should_abort():
            raise TransferAborted("传输已取消")
        connection: EpdConnection | None = None
        phase = "connect"
        try:
            connection = await EpdConnection.connect(device_name, timeout=connect_timeout)
            info = await connection.initialize()
            phase = "transfer"
            if should_abort and should_abort():
                raise TransferAborted("传输已取消")
            if info.voltage_mv is not None and info.voltage_mv <= 2800:
                raise LowBattery(f"看板电压 {info.voltage_mv}mV，已暂停自动传图")
            frame = await frame_factory(info)
            refresh_requested, uncertain = await connection.send_frame(
                frame, should_abort=should_abort
            )
            _log.info(
                "ble attempt=%d/%d ok elapsed=%.1fs",
                attempt + 1,
                attempts,
                time.monotonic() - started,
            )
            return TransferResult(
                info=info,
                layout=frame,
                refresh_requested=refresh_requested,
                refresh_uncertain=uncertain,
            )
        except LowBattery:
            raise
        except (BleakError, TimeoutError, OSError) as exc:
            last_error = exc
            last_error_was_connect = phase == "connect"
            _log.warning(
                "ble attempt=%d/%d failed elapsed=%.1fs error=%s",
                attempt + 1,
                attempts,
                time.monotonic() - started,
                type(exc).__name__,
            )
        except BleTransferError as exc:
            last_error = exc
            last_error_was_connect = phase == "connect"
            _log.warning(
                "ble attempt=%d/%d failed elapsed=%.1fs error=%s",
                attempt + 1,
                attempts,
                time.monotonic() - started,
                type(exc).__name__,
            )
        finally:
            if connection is not None:
                await connection.close()
        if attempt + 1 < attempts:
            await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    if isinstance(last_error, DeviceNotFound):
        raise last_error
    if last_error_was_connect and isinstance(last_error, (BleakError, TimeoutError, OSError)):
        # Windows 的连接卡死只有重启本机蓝牙能稳定恢复，看板本身不用断电。
        raise BleConnectTimeout("连接看板超时") from last_error
    raise BleTransferError(f"BLE 传输失败：{type(last_error).__name__}") from last_error
