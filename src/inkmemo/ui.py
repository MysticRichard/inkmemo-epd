from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

import pystray
from PIL import Image, ImageDraw

from .storage import SecretStore, Settings, reset_source_cache, validate_wps_url
from .windows import read_autostart, set_autostart

if TYPE_CHECKING:
    from .runtime import SyncService

logger = logging.getLogger(__name__)


def _icon_image() -> Image.Image:
    image = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((4, 4, 59, 59), outline="black", width=4)
    draw.rectangle((10, 10, 53, 25), fill="black")
    draw.rectangle((10, 31, 31, 53), outline="black", width=3)
    draw.rectangle((35, 31, 53, 37), fill="#cf1b1b")
    draw.rectangle((35, 42, 53, 48), fill="black")
    draw.rectangle((35, 51, 53, 53), fill="black")
    return image


class InkMemoApp:
    def __init__(self, service: SyncService) -> None:
        self.service = service
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="inkmemo-runtime", daemon=True)
        self._raise_settings = threading.Event()
        self._settings_queue: queue.Queue[None] = queue.Queue()
        self._settings_open = False
        threading.Thread(
            target=self._settings_worker, name="inkmemo-settings", daemon=True
        ).start()
        self.icon = pystray.Icon(
            "InkMemo",
            _icon_image(),
            "墨笺 InkMemo",
            menu=pystray.Menu(
                pystray.MenuItem(self._status_label, None, enabled=False),
                pystray.MenuItem("立即同步", self.sync, default=True),
                pystray.MenuItem("强制重发", self.force_send),
                pystray.MenuItem("查看预览", self.open_preview),
                pystray.MenuItem("设置 / 更换 WPS 链接", self.open_settings),
                pystray.MenuItem(self._pause_label, self.toggle_pause),
                pystray.MenuItem("退出", self.quit),
            ),
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.service.run_forever())
        self.loop.create_task(self._watch_settings_requests())
        self.loop.run_forever()

    async def _watch_settings_requests(self) -> None:
        from .windows import consume_settings_request

        while not self.service.stop_event.is_set():
            if consume_settings_request(self.service.base_dir):
                self.open_settings()
            await asyncio.sleep(1)

    def _status_label(self, _item=None) -> str:
        state = self.service.state.read()
        paused = self.service.settings.paused
        source = "云端正常" if state.get("source_health") == "ok" else "云端待检查"
        ble = state.get("ble_health", "未连接")
        count = state.get("last_task_count", 0)
        return f"{'已暂停' if paused else source} · {count} 项 · BLE {ble}"

    def _pause_label(self, _item=None) -> str:
        return "恢复自动同步" if self.service.settings.paused else "暂停自动同步"

    def sync(self, _icon=None, _item=None) -> None:
        asyncio.run_coroutine_threadsafe(self.service.check(), self.loop)

    def force_send(self, _icon=None, _item=None) -> None:
        asyncio.run_coroutine_threadsafe(self.service.check(force_send=True), self.loop)

    def toggle_pause(self, _icon=None, _item=None) -> None:
        if self.service.settings.paused:
            self.service.resume()
        else:
            self.service.pause()
        self.icon.update_menu()

    def open_preview(self, _icon=None, _item=None) -> None:
        path = self.service.preview_dir / "latest.png"
        if path.exists():
            os.startfile(path)
        else:
            self.icon.notify("尚无预览，请先执行一次同步", "墨笺 InkMemo")

    def notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            logger.warning("system notification failed", exc_info=True)

    def open_settings(self, _icon=None, _item=None) -> None:
        if self._settings_open:
            self._raise_settings.set()
            return
        self._settings_open = True
        self._settings_queue.put(None)

    def _settings_worker(self) -> None:
        # 一个进程只建一个 Tk 解释器：反复创建/销毁或跨线程使用会让 Tcl 崩溃。
        root = tk.Tk()
        root.title("墨笺 InkMemo 设置")
        root.geometry("640x620")
        root.resizable(False, False)
        root.withdraw()
        refresh = self._build_settings(root)

        def hide() -> None:
            root.withdraw()
            self._settings_open = False

        root.protocol("WM_DELETE_WINDOW", hide)

        def pump() -> None:
            show = False
            while True:
                try:
                    self._settings_queue.get_nowait()
                except queue.Empty:
                    break
                show = True
            if self._raise_settings.is_set():
                self._raise_settings.clear()
                show = True
            if show:
                refresh()
                root.deiconify()
                root.lift()
                root.focus_force()
                root.attributes("-topmost", True)
                # 置顶要覆盖窗口映射过程，否则会被留在其他窗口后面。
                root.after(1500, lambda: root.attributes("-topmost", False))
            root.after(200, pump)

        root.after(200, pump)
        try:
            root.mainloop()
        finally:
            self._settings_open = False

    def _build_settings(self, root: tk.Tk):
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)

        secrets = SecretStore(self.service.base_dir)
        link_value = tk.StringVar()
        link_hint = tk.StringVar()
        board_year_var = tk.StringVar()
        city_var = tk.StringVar()
        interval_var = tk.StringVar()
        device_value = tk.StringVar()
        scan_status = tk.StringVar()
        status_var = tk.StringVar()
        autostart_state = {"value": False}
        autostart_var = tk.BooleanVar()
        paused_var = tk.BooleanVar()

        ttk.Label(frame, text="WPS 文档链接", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        link_entry = ttk.Entry(frame, textvariable=link_value, width=66, show="•")
        link_entry.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 3))
        ttk.Label(
            frame,
            textvariable=link_hint,
            foreground="#555555",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

        fields = ttk.Frame(frame)
        fields.grid(row=3, column=0, columnspan=3, sticky="ew")
        for index, (label, var) in enumerate(
            [
                ("看板年度", board_year_var),
                ("城市", city_var),
                ("检查间隔（秒）", interval_var),
            ]
        ):
            ttk.Label(fields, text=label).grid(row=index, column=0, sticky="w", pady=3)
            ttk.Entry(fields, textvariable=var, width=30).grid(
                row=index, column=1, sticky="w", padx=12, pady=3
            )

        ttk.Label(fields, text="看板蓝牙名称").grid(row=3, column=0, sticky="w", pady=3)
        device_box = ttk.Combobox(fields, textvariable=device_value, width=24)
        device_box.grid(row=3, column=1, sticky="w", padx=12, pady=3)
        scan_button = ttk.Button(fields, text="扫描看板", width=9)
        scan_button.grid(row=3, column=2, sticky="w")
        ttk.Label(fields, textvariable=scan_status, foreground="#555555", wraplength=520).grid(
            row=4, column=1, columnspan=2, sticky="w", padx=12
        )

        def scan_devices() -> None:
            from .ble.client import scan_boards
            from .ble.lease import BleLeaseBusy, acquire_ble_lease

            async def discover() -> list[str]:
                async with acquire_ble_lease("inkmemo-settings-scan"):
                    return await scan_boards()

            scan_button.state(["disabled"])
            scan_status.set("正在扫描，约 15 秒……")

            def work() -> None:
                try:
                    names = asyncio.run(discover())
                    message = f"发现 {len(names)} 个看板：" + "、".join(names) if names else "没发现 NRF_EPD_ 看板，请确认已上电并在附近"
                except BleLeaseBusy as exc:
                    names, message = [], str(exc)
                except Exception as exc:
                    names, message = [], f"扫描失败：{type(exc).__name__}"

                def apply() -> None:
                    if names:
                        device_box["values"] = names
                        if not device_value.get().strip():
                            device_value.set(names[0])
                    scan_status.set(message)
                    scan_button.state(["!disabled"])

                root.after(0, apply)

            threading.Thread(target=work, daemon=True).start()

        scan_button.configure(command=scan_devices)

        def sync_autostart_state() -> None:
            # PowerShell 查询要 1-4 秒；放到线程里，窗口先能正常输入和关闭。
            result: dict[str, bool] = {}

            def work() -> None:
                try:
                    result["value"] = autostart_state["value"] = read_autostart()
                except Exception:
                    return

            threading.Thread(target=work, daemon=True).start()

            def poll() -> None:
                if "value" in result:
                    autostart_var.set(result["value"])
                else:
                    root.after(150, poll)

            root.after(150, poll)

        ttk.Checkbutton(fields, text="登录 Windows 后自动启动", variable=autostart_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 2)
        )
        ttk.Checkbutton(fields, text="暂停自动同步", variable=paused_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=2
        )
        fields.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            textvariable=status_var,
            justify="left",
            foreground="#333333",
            wraplength=570,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 12))

        def save() -> bool:
            try:
                settings = Settings.load(self.service.base_dir)
                new_url = link_value.get().strip()
                if new_url:
                    validate_wps_url(new_url)
                board_year = int(board_year_var.get()) if board_year_var.get().strip() else None
                city = city_var.get().strip() or "上海"
                interval_seconds = max(30, int(interval_var.get()))
                device_name = device_value.get().strip()
                if not device_name:
                    raise ValueError("请先点“扫描看板”，选中你的看板后再保存")
                paused = paused_var.get()
                autostart = autostart_var.get()

                if autostart != autostart_state["value"]:
                    status_var.set("正在设置登录自启，请稍候……")
                    root.update_idletasks()
                    set_autostart(autostart)
                    autostart_state["value"] = autostart
                if new_url:
                    secrets.set_url(new_url)
                    self._reset_source_cache()
                settings.board_year = board_year
                settings.city = city
                settings.interval_seconds = interval_seconds
                settings.device_name = device_name
                settings.paused = paused
                settings.autostart = autostart
                settings.save(self.service.base_dir)
                self.service.settings = settings
                self.service.state.update(
                    service_health="paused" if settings.paused else "active"
                )
                link_value.set("")
                status_var.set(self._details_status())
                self.loop.call_soon_threadsafe(self.service.force_event.set)
                messagebox.showinfo("墨笺 InkMemo", "设置已保存")
                return True
            except Exception as exc:
                messagebox.showerror("设置保存失败", str(exc))
                return False

        def test() -> None:
            if not save():
                return
            asyncio.run_coroutine_threadsafe(self.service.check(), self.loop)
            messagebox.showinfo("墨笺 InkMemo", "已开始测试读取；结果会通过系统通知和状态显示")

        def login() -> None:
            try:
                new_url = link_value.get().strip()
                if new_url:
                    validate_wps_url(new_url)
                    secrets.set_url(new_url)
                    self._reset_source_cache()
                    link_value.set("")
                url = secrets.get_url()
                if not url:
                    messagebox.showwarning("WPS 授权", "请先保存 WPS 链接")
                    return
            except Exception as exc:
                messagebox.showerror("WPS 授权", str(exc))
                return

            def run_login() -> None:
                asyncio.run_coroutine_threadsafe(
                    self.service.reader.login(url),
                    self.loop,
                ).result()

            threading.Thread(target=run_login, daemon=True).start()

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Button(buttons, text="保存", command=save).pack(side="left")
        ttk.Button(buttons, text="保存并测试连接", command=test).pack(side="left", padx=8)
        ttk.Button(buttons, text="WPS 授权", command=login).pack(side="left", padx=8)
        ttk.Button(buttons, text="立即同步", command=lambda: self.sync()).pack(side="left", padx=8)
        ttk.Button(buttons, text="强制重发", command=lambda: self.force_send()).pack(side="left")
        ttk.Button(buttons, text="查看预览", command=self.open_preview).pack(side="right")

        def refresh() -> None:
            current = Settings.load(self.service.base_dir)
            has_url = bool(secrets.get_url())
            link_value.set("")
            link_hint.set(
                "已保存受保护链接；留空表示不修改" if has_url else "尚未配置 WPS 链接"
            )
            board_year_var.set("" if current.board_year is None else str(current.board_year))
            city_var.set(current.city)
            interval_var.set(str(current.interval_seconds))
            device_value.set(current.device_name)
            # 自启状态以系统计划任务为准，保存时也要和它比较后再决定是否写入。
            autostart_state["value"] = current.autostart
            autostart_var.set(current.autostart)
            paused_var.set(current.paused)
            scan_status.set(
                "点“扫描看板”，从发现的设备里选一个；不用自己输名字"
                if not current.device_name
                else ""
            )
            status_var.set(self._details_status())
            sync_autostart_state()

        frame.columnconfigure(0, weight=1)
        root.after(1000, lambda: self._refresh_status_loop(root, status_var))
        return refresh

    def _refresh_status_loop(self, root: tk.Tk, status_var: tk.StringVar) -> None:
        if not root.winfo_exists():
            return
        status_var.set(self._details_status())
        root.after(1500, lambda: self._refresh_status_loop(root, status_var))

    def _details_status(self) -> str:
        state = self.service.state.read()
        return (
            f"最后检查：{state.get('last_check_at', '--')}\n"
            f"事项：{state.get('last_task_count', 0)} 项 · 版式：{state.get('last_layout', '--')}\n"
            f"云端：{state.get('source_health', '--')} · BLE：{state.get('ble_health', '--')} · "
            f"显示：{state.get('display_health', '--')}"
        )

    def _reset_source_cache(self) -> None:
        reset_source_cache(self.service.base_dir, self.service.state)

    def quit(self, _icon=None, _item=None) -> None:
        async def shutdown() -> None:
            await self.service.shutdown()
            self.loop.stop()

        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.icon.stop()

    def run(self) -> None:
        self.thread.start()
        self.icon.run()
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
