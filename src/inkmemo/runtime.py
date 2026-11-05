from __future__ import annotations

import asyncio
import hashlib
import logging
import logging.handlers
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .ble.client import (
    BleConnectTimeout,
    BleTransferError,
    DeviceNotFound,
    LowBattery,
    TransferAborted,
    transfer_with_retries,
)
from .ble.lease import BleLeaseBusy
from .domain import SourceFormatError, content_hash, parse_grid, sort_tasks
from .models import Battery, LayoutResult, SyncResult, Weather
from .render.layouts import HtmlRenderer, LayoutOverflow, battery_from_mv, overlay_failure_footer
from .sources.wps import WpsAuthError, WpsReader, WpsSourceError
from .storage import APP_DIR, RuntimeState, SecretStore, Settings, _atomic_write
from .weather import fetch_weather, resolve_weather_location


Notify = Callable[[str, str], None]
LOOP_ERROR_DELAY = 30.0
BLE_RECONNECT_HINT = "请在 Windows 设置 → 蓝牙 中关闭再开启蓝牙，然后重试；屏幕保持原画面"


class SyncService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_dir: Path = APP_DIR,
        notify: Notify | None = None,
    ) -> None:
        self.base_dir = base_dir
        self.settings = settings or Settings.load(base_dir)
        self.secrets = SecretStore(base_dir)
        self.state = RuntimeState(base_dir)
        self.notify = notify or (lambda _title, _message: None)
        self.reader = WpsReader(base_dir / "browser-profile")
        self.renderer = HtmlRenderer()
        self.preview_dir = base_dir / "previews"
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        self.force_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self.logger = _build_logger(base_dir)
        self._loop: asyncio.AbstractEventLoop | None = None

    async def close(self) -> None:
        await self.reader.close()
        await self.renderer.close()

    async def shutdown(self) -> None:
        self.stop_event.set()
        self.wake()
        async with self._check_lock:
            pass
        await self.close()

    def wake(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.force_event.set)
        else:
            self.force_event.set()

    def pause(self) -> None:
        self.settings.paused = True
        self.settings.save(self.base_dir)
        self.state.update(service_health="paused")
        self.wake()
        self.notify("墨笺已暂停", "暂停期间不会取数或连接看板")

    def resume(self) -> None:
        self.settings.paused = False
        self.settings.save(self.base_dir)
        self.state.update(service_health="active")
        self.wake()
        self.notify("墨笺已恢复", "将立即检查一次最新内容")

    def _reload_settings(self) -> None:
        # Settings can be changed from another process (CLI pause, set-link,
        # set-year); the running service must not keep the values it loaded at
        # startup.
        try:
            self.settings = Settings.load(self.base_dir)
        except Exception:
            self.logger.warning("settings reload failed")

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            while not self.stop_event.is_set():
                try:
                    self._reload_settings()
                    self.state.update(service_health="paused" if self.settings.paused else "active")
                    if not self.settings.paused:
                        try:
                            await self.check()
                        except Exception:
                            self.logger.exception("unexpected sync failure")
                            self.state.update(
                                service_health="error",
                                last_error_class="unexpected",
                            )
                            self.notify("墨笺同步异常", "后台已记录错误，将按计划重试")
                    try:
                        await asyncio.wait_for(
                            self._next_wake(),
                            timeout=max(30, int(self.settings.interval_seconds)),
                        )
                    except asyncio.TimeoutError:
                        pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient disk/OS error here used to kill the loop and
                    # leave a tray icon that never syncs again.
                    self.logger.exception("sync loop error")
                    try:
                        self.state.update(service_health="error", last_error_class="loop")
                    except Exception:
                        pass
                    await asyncio.sleep(LOOP_ERROR_DELAY)
        finally:
            self._loop = None

    async def _next_wake(self) -> None:
        force = asyncio.create_task(self.force_event.wait())
        stop = asyncio.create_task(self.stop_event.wait())
        done: set[asyncio.Task] = set()
        settings_path = self.base_dir / "settings.json"
        try:
            settings_stamp = settings_path.stat().st_mtime_ns
        except OSError:
            settings_stamp = None
        try:
            while True:
                done, _ = await asyncio.wait(
                    {force, stop},
                    timeout=1.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break
                try:
                    current_stamp = settings_path.stat().st_mtime_ns
                except OSError:
                    current_stamp = None
                if current_stamp != settings_stamp:
                    break
        finally:
            force.cancel()
            stop.cancel()
        self.force_event.clear()
        if stop in done:
            return

    async def check(self, *, force_send: bool = False, preview_only: bool = False) -> SyncResult:
        if self.settings.paused:
            return SyncResult(False, "paused", "服务已暂停")
        if self.stop_event.is_set():
            return SyncResult(False, "stopped", "服务正在退出")
        if self._check_lock.locked():
            return SyncResult(False, "busy", "已有同步任务正在执行")
        async with self._check_lock:
            now = datetime.now().astimezone()
            self.state.update(last_check_at=now.isoformat(), service_health="active")
            return await self._check_inner(now, force_send=force_send, preview_only=preview_only)

    async def _check_inner(
        self, now: datetime, *, force_send: bool, preview_only: bool
    ) -> SyncResult:
        url = self.secrets.get_url()
        if not url:
            self.state.update(source_health="config_error", last_error_class="missing_url")
            return SyncResult(False, "source", "尚未配置 WPS 链接")

        aborted = self._abort_reason()
        if aborted:
            return SyncResult(False, aborted, "服务已暂停" if aborted == "paused" else "服务正在退出")

        try:
            grid = await self.reader.read(url)
        except WpsAuthError as exc:
            self.logger.warning("source auth error")
            return await self._source_failed(
                "auth", "WPS 会话失效，需要重新登录", force_send, preview_only
            )
        except WpsSourceError as exc:
            self.logger.warning("source error %s", type(exc).__name__)
            return await self._source_failed(
                "cloud", "WPS 云端读取失败", force_send, preview_only
            )
        except Exception as exc:
            self.logger.warning("source unexpected %s", type(exc).__name__)
            return await self._source_failed(
                "cloud", "WPS 云端读取失败", force_send, preview_only
            )

        try:
            tasks = sort_tasks(
                parse_grid(grid, self.settings.board_year, date.today(), self.settings.near_days)
            )
        except (SourceFormatError, ValueError) as exc:
            self.logger.warning("format error %s", type(exc).__name__)
            return await self._source_failed(
                "format", "WPS 表头或内容格式不完整", force_send, preview_only
            )

        location = await resolve_weather_location(
            self.settings.city,
            self.settings.latitude,
            self.settings.longitude,
        )
        weather = await asyncio.to_thread(
            fetch_weather,
            location.city,
            location.latitude,
            location.longitude,
        )
        state = self.state.read()
        last_mv = state.get("last_voltage_mv")
        battery = battery_from_mv(int(last_mv) if isinstance(last_mv, int) else None)
        try:
            preview = await self.renderer.render(
                tasks,
                date.today(),
                grid.fetched_at,
                weather,
                battery,
            )
        except LayoutOverflow as exc:
            self.logger.warning("layout overflow count=%d", len(tasks))
            self.state.update(
                source_health="ok",
                last_source_success_at=now.isoformat(),
                last_task_count=len(tasks),
                last_error_class="layout_overflow",
            )
            self.notify(
                "版面放不下",
                "事项太长或超过 30 项，屏幕保持上一张画面；请精简事项文字后重试",
            )
            return SyncResult(False, "render", str(exc), task_count=len(tasks))

        _atomic_write(self.preview_dir / "latest.png", preview.png)
        _atomic_write(self.preview_dir / "last_good.png", preview.png)
        self.state.update(
            source_health="ok",
            last_source_success_at=now.isoformat(),
            last_good_at=grid.fetched_at.isoformat(),
            last_task_count=len(tasks),
            last_layout=preview.layout,
            last_weather_ok=weather.available,
            source_hash=content_hash(tasks, self.settings.board_year)[:16],
            last_error_class=None,
        )
        if preview_only:
            return SyncResult(
                True,
                "preview",
                "已生成本地预览，未连接看板",
                task_count=len(tasks),
                layout=preview.layout,
            )

        previous_sent = self.state.read().get("last_sent_display_hash")
        if not force_send and previous_sent == preview.display_hash:
            return SyncResult(
                True,
                "unchanged",
                "内容未变化，不重复刷新",
                task_count=len(tasks),
                layout=preview.layout,
            )

        aborted = self._abort_reason()
        if aborted:
            return SyncResult(False, aborted, "服务已暂停" if aborted == "paused" else "服务正在退出")

        async def frame_factory(info):
            aborted = self._abort_reason()
            if aborted:
                raise TransferAborted("传输已取消")
            voltage_mv = info.voltage_mv
            if voltage_mv is None and isinstance(last_mv, int):
                voltage_mv = last_mv
            if info.voltage_mv is not None:
                self.state.update(last_voltage_mv=info.voltage_mv)
            return await self.renderer.render(
                tasks,
                date.today(),
                grid.fetched_at,
                weather,
                battery_from_mv(voltage_mv),
            )

        try:
            transferred = await transfer_with_retries(
                self.settings.device_name,
                frame_factory,
                should_abort=lambda: self._abort_reason() is not None,
            )
        except TransferAborted:
            aborted = self._abort_reason() or "stopped"
            self.state.update(ble_health="aborted", display_health="kept_previous")
            return SyncResult(
                False,
                aborted,
                "服务已暂停" if aborted == "paused" else "服务正在退出",
                task_count=len(tasks),
                layout=preview.layout,
            )
        except BleLeaseBusy:
            self.state.update(
                ble_health="busy",
                display_health="kept_previous",
                last_error_class="ble_busy",
            )
            self.notify("蓝牙被占用", "LCD 看板正在使用蓝牙；请稍后重试，屏幕保持原画面")
            return SyncResult(
                False,
                "ble",
                "蓝牙被其他程序占用",
                task_count=len(tasks),
                layout=preview.layout,
            )
        except DeviceNotFound:
            self.state.update(
                ble_health="not_found",
                display_health="kept_previous",
                last_error_class="ble_not_found",
            )
            self.notify("未发现看板", f"未发现 {self.settings.device_name}，屏幕保持原画面")
            return SyncResult(False, "ble", "未发现目标看板", task_count=len(tasks), layout=preview.layout)
        except LowBattery as exc:
            self.state.update(
                ble_health="low_battery",
                display_health="kept_previous",
                last_error_class="low_battery",
            )
            self.notify("看板电量低", str(exc))
            return SyncResult(False, "ble", str(exc), task_count=len(tasks), layout=preview.layout)
        except BleConnectTimeout:
            self.state.update(
                ble_health="connect_timeout",
                display_health="kept_previous",
                last_error_class="ble_connect_timeout",
            )
            self.notify("蓝牙连接超时", BLE_RECONNECT_HINT)
            return SyncResult(False, "ble", "蓝牙连接超时", task_count=len(tasks), layout=preview.layout)
        except BleTransferError as exc:
            self.logger.warning("ble error %s", type(exc).__name__)
            self.state.update(
                ble_health="failed",
                display_health="kept_previous",
                last_error_class="ble_failed",
            )
            self.notify("蓝牙同步失败", "屏幕保持原画面，请检查看板电源与距离")
            return SyncResult(False, "ble", "蓝牙传输失败", task_count=len(tasks), layout=preview.layout)

        _atomic_write(self.preview_dir / "latest.png", transferred.layout.png)
        _atomic_write(self.preview_dir / "last_good.png", transferred.layout.png)
        _atomic_write(self.preview_dir / "last_sent.png", transferred.layout.png)
        voltage_mv = (
            transferred.info.voltage_mv
            if transferred.info.voltage_mv is not None
            else last_mv if isinstance(last_mv, int) else None
        )
        self.state.update(
            ble_health="ok",
            display_health="refresh_uncertain" if transferred.refresh_uncertain else "refresh_requested",
            last_transfer_confirmed_at=datetime.now().astimezone().isoformat(),
            last_refresh_assumed_at=datetime.now().astimezone().isoformat(),
            last_sent_display_hash=transferred.layout.display_hash,
            last_voltage_mv=voltage_mv,
            last_error_class=None,
        )
        return SyncResult(
            True,
            "display",
            "黑红两层已确认，已请求刷新；屏幕显示仍需目视确认",
            task_count=len(tasks),
            layout=transferred.layout.layout,
            sent=True,
        )

    async def _source_failed(
        self,
        error_class: str,
        message: str,
        force_send: bool,
        preview_only: bool = False,
    ) -> SyncResult:
        self.state.update(source_health="failed", last_error_class=error_class)
        self.notify(
            "WPS 读取失败",
            message + ("；已生成本地失败预览" if preview_only else "；正在尝试显示上次成功画面"),
        )
        aborted = self._abort_reason()
        if aborted:
            return SyncResult(False, aborted, "服务已暂停" if aborted == "paused" else "服务正在退出")
        state = self.state.read()
        cached = self.preview_dir / "last_sent.png"
        # The footer time must describe the frame being shown, not the latest
        # render that never reached the screen.
        stamp = state.get("last_transfer_confirmed_at")
        if not cached.exists():
            cached = self.preview_dir / "last_good.png"
            stamp = state.get("last_good_at")
        try:
            cached_at = datetime.fromisoformat(stamp) if stamp else datetime.now().astimezone()
        except ValueError:
            cached_at = datetime.now().astimezone()
        if cached.exists():
            frame = overlay_failure_footer(
                cached.read_bytes(),
                "云端读取失败 · 显示上次成功画面",
                cached_at,
            )
        else:
            weather = Weather.unavailable(self.settings.city)
            frame = await self.renderer.render(
                [],
                date.today(),
                datetime.now().astimezone(),
                weather,
                Battery(None),
                status="云端读取失败 · 请检查链接或重新登录",
                force_layout="error",
            )
        _atomic_write(self.preview_dir / "error.png", frame.png)
        if preview_only:
            return SyncResult(False, "source", message + "；已生成失败预览", layout=frame.layout)
        prior = state.get("last_sent_display_hash")
        if not force_send and prior == frame.display_hash:
            return SyncResult(False, "source", message + "；失败画面已显示", layout=frame.layout)
        try:
            transferred = await transfer_with_retries(
                self.settings.device_name,
                lambda _info: _ready(frame),
                should_abort=lambda: self._abort_reason() is not None,
            )
        except TransferAborted:
            aborted = self._abort_reason() or "stopped"
            self.state.update(ble_health="aborted", display_health="kept_previous")
            return SyncResult(
                False,
                aborted,
                "服务已暂停" if aborted == "paused" else "服务正在退出",
                layout=frame.layout,
            )
        except BleLeaseBusy:
            self.state.update(
                ble_health="busy",
                display_health="kept_previous",
                last_error_class="ble_busy",
            )
            self.notify("蓝牙被占用", "LCD 看板正在使用蓝牙；请稍后重试，屏幕保持原画面")
            return SyncResult(
                False,
                "ble",
                message + "；蓝牙被其他程序占用",
                layout=frame.layout,
            )
        except BleConnectTimeout:
            self.state.update(
                ble_health="connect_timeout",
                display_health="kept_previous",
                last_error_class="ble_connect_timeout",
            )
            self.notify("蓝牙连接超时", BLE_RECONNECT_HINT)
            return SyncResult(False, "ble", message + "；蓝牙连接超时", layout=frame.layout)
        except Exception as exc:
            self.logger.warning("cached error transfer %s", type(exc).__name__)
            self.state.update(
                ble_health="failed",
                display_health="kept_previous",
                last_error_class="ble_failed",
            )
            self.notify("蓝牙同步失败", "缓存画面未能发送，屏幕保持上一张画面")
            return SyncResult(False, "ble", message + "；缓存画面未能发送", layout=frame.layout)
        self.state.update(
            ble_health="ok",
            display_health="refresh_uncertain" if transferred.refresh_uncertain else "refresh_requested",
            last_transfer_confirmed_at=datetime.now().astimezone().isoformat(),
            last_refresh_assumed_at=datetime.now().astimezone().isoformat(),
            last_sent_display_hash=transferred.layout.display_hash,
            last_error_class=error_class,
        )
        return SyncResult(True, "cached", message + "；已发送缓存画面", layout=frame.layout, sent=True)

    def _abort_reason(self) -> str | None:
        if self.stop_event.is_set():
            return "stopped"
        if self.settings.paused:
            return "paused"
        return None


async def _ready(frame: LayoutResult) -> LayoutResult:
    return frame


def _build_logger(base_dir: Path) -> logging.Logger:
    logger = logging.getLogger("inkmemo")
    if logger.handlers:
        return logger
    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "inkmemo.log",
        maxBytes=512_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
