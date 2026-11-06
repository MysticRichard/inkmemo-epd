from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .ble.lease import BleLeaseBusy, acquire_ble_lease
from .runtime import SyncService
from .storage import (
    APP_DIR,
    RuntimeState,
    SecretStore,
    Settings,
    reset_source_cache,
    validate_wps_url,
)
from .windows import SingleInstance, create_desktop_shortcut, request_settings, set_autostart


def _startup_trace(note: str) -> None:
    # Autostart failures are invisible in a windowed build: log the fact that
    # the process was reached before anything else can fail.
    try:
        path = APP_DIR / "startup.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{datetime.now().astimezone().isoformat()} pid={os.getpid()} "
                f"cwd={os.getcwd()} {note}\n"
            )
    except Exception:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inkmemo", description="墨笺 InkMemo")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="后台托盘运行")
    sync = sub.add_parser("sync", help="立即执行一次同步")
    sync.add_argument("--force", action="store_true", help="忽略内容去重强制重发")
    sub.add_parser("preview", help="读取云端并生成本地预览，不连接看板")
    sub.add_parser("calibrate", help="发送真机方向/三色校准图")
    sub.add_parser("settings", help="打开设置")
    sub.add_parser("scan", help="扫描 BLE 设备")
    set_link = sub.add_parser("set-link", help="保存 WPS 链接")
    set_link.add_argument("url")
    login = sub.add_parser("login", help="打开浏览器重新授权 WPS 会话")
    login.add_argument("--url")
    set_year = sub.add_parser("set-year", help="设置看板年度")
    set_year.add_argument("year", type=int)
    pause = sub.add_parser("pause", help="暂停自动同步")
    pause.add_argument("--resume", action="store_true")
    auto = sub.add_parser("autostart", help="设置登录自启")
    auto.add_argument("state", choices=("on", "off"))
    sub.add_parser("status", help="显示脱敏状态")
    return parser


async def _sync_once(force: bool = False, preview_only: bool = False) -> int:
    service = SyncService()
    try:
        result = await service.check(force_send=force, preview_only=preview_only)
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "stage": result.stage,
                    "message": result.message,
                    "task_count": result.task_count,
                    "layout": result.layout,
                    "sent": result.sent,
                },
                ensure_ascii=False,
            )
        )
        return 0 if result.ok else 1
    finally:
        await service.close()


async def _scan() -> int:
    from bleak import BleakScanner

    try:
        async with acquire_ble_lease("inkmemo-scan"):
            devices = await BleakScanner.discover(timeout=20, return_adv=True)
            rows = []
            for device, adv in devices.values():
                name = device.name or adv.local_name
                if name and name.startswith("NRF_EPD_"):
                    rows.append({"name": name, "address": device.address, "rssi": adv.rssi})
    except BleLeaseBusy as error:
        print(
            json.dumps(
                {"ok": False, "state": "busy", "message": str(error)},
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


async def _calibrate() -> int:
    from .ble.client import transfer_with_retries
    from .render.calibration import create_calibration
    from .render.layouts import battery_from_mv

    settings = Settings.load()
    frame = create_calibration()

    async def factory(info):
        return frame

    try:
        result = await transfer_with_retries(settings.device_name, factory)
        config = result.info.config
        print(
            json.dumps(
                {
                    "ok": True,
                    "version": f"0x{result.info.version:02x}",
                    "voltage_mv": result.info.voltage_mv,
                    "refresh_requested": result.refresh_requested,
                    "model_id": config.model_id if config else None,
                    "wakeup_pin": f"0x{config.wakeup_pin:02x}" if config else None,
                    "display_mode": config.display_mode if config else None,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False))
        return 1


def _run_tray() -> int:
    from .ui import InkMemoApp

    _startup_trace("tray start")
    instance = SingleInstance()
    if not instance.acquire():
        _startup_trace("secondary instance; settings requested")
        request_settings()
        return 0
    try:
        try:
            create_desktop_shortcut()
        except Exception:
            _startup_trace("desktop shortcut failed:\n" + traceback.format_exc())
        loop_service = SyncService()
        app = InkMemoApp(loop_service)
        app.service.notify = app.notify
        if not SecretStore().get_url():
            import threading
            import time

            def open_first_run() -> None:
                time.sleep(1)
                app.open_settings()

            threading.Thread(target=open_first_run, daemon=True).start()
        app.run()
        _startup_trace("tray exited")
        return 0
    except Exception:
        _startup_trace("tray crashed:\n" + traceback.format_exc())
        raise
    finally:
        instance.release()


async def _login(url: str | None) -> int:
    from .sources.wps import WpsReader

    store = SecretStore()
    target = url or store.get_url()
    if not target:
        raise ValueError("尚未配置 WPS 链接")
    if url:
        store.set_url(validate_wps_url(url))
        reset_source_cache()
    print("请在弹出的浏览器中完成 WPS 登录或权限确认，完成后关闭浏览器窗口。")
    reader = WpsReader()
    try:
        await reader.login(target)
    finally:
        await reader.close()
    return 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    command = args.command or "run"
    if command == "run":
        return _run_tray()
    if command in ("sync", "preview"):
        from .windows import service_running

        if service_running():
            # Two processes cannot share the browser profile: the second one
            # gets TargetClosedError and can push an error frame to the board.
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "busy",
                        "message": "后台服务正在运行：请用托盘菜单“强制重发”，或先退出托盘再执行命令",
                    },
                    ensure_ascii=False,
                )
            )
            return 2
    if command == "sync":
        return asyncio.run(_sync_once(args.force))
    if command == "preview":
        return asyncio.run(_sync_once(preview_only=True))
    if command == "scan":
        return asyncio.run(_scan())
    if command == "calibrate":
        return asyncio.run(_calibrate())
    if command == "settings":
        request_settings()
        return 0
    if command == "set-link":
        SecretStore().set_url(args.url)
        reset_source_cache()
        return 0
    if command == "login":
        return asyncio.run(_login(args.url))
    if command == "set-year":
        settings = Settings.load()
        settings.board_year = args.year
        settings.save()
        return 0
    if command == "pause":
        settings = Settings.load()
        settings.paused = not args.resume
        settings.save()
        RuntimeState().update(service_health="paused" if settings.paused else "active")
        return 0
    if command == "autostart":
        enabled = args.state == "on"
        try:
            set_autostart(enabled)
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            message = f"登录自启设置失败，未保存本次设置。\n{error}"
            if getattr(sys, "frozen", False):
                from tkinter import messagebox

                messagebox.showerror("墨笺 InkMemo 自启设置失败", message)
            else:
                print(json.dumps(
                    {"ok": False, "stage": "autostart", "message": message},
                    ensure_ascii=False,
                ))
            return 1
        settings = Settings.load()
        settings.autostart = enabled
        settings.save()
        return 0
    if command == "status":
        print(json.dumps(RuntimeState().read(), ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
