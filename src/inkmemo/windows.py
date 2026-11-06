from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
from pathlib import Path

from .storage import APP_DIR


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\InkMemoSingleInstance"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "InkMemo"
AUTOSTART_TASK_NAME = "InkMemo"
SHORTCUT_NAME = "墨笺 InkMemo.lnk"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SingleInstance:
    def __init__(self) -> None:
        self.handle = None

    def acquire(self) -> bool:
        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def service_running() -> bool:
    # The tray keeps the browser profile and BLE adapter; a second process
    # reading the same profile fails with TargetClosedError and can push an
    # error frame to the board.
    handle = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def request_settings(base_dir: Path = APP_DIR) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "show-settings.request").touch()


def consume_settings_request(base_dir: Path = APP_DIR) -> bool:
    path = base_dir / "show-settings.request"
    if not path.exists():
        return False
    path.unlink(missing_ok=True)
    return True


def _startup_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "run"]
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else python
    return [str(executable), "-m", "inkmemo", "run"]


def startup_command() -> str:
    return subprocess.list2cmdline(_startup_args())


def _powershell_args(script: str) -> list[str]:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        + script
    )
    return [
        str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
    ]


def build_autostart_create_args() -> list[str]:
    executable, *arguments = _startup_args()
    executable_literal = executable.replace("'", "''")
    arguments_literal = subprocess.list2cmdline(arguments).replace("'", "''")
    return _powershell_args(f"""
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$executable = '{executable_literal}'
$action = New-ScheduledTaskAction -Execute $executable -Argument '{arguments_literal}' -WorkingDirectory (Split-Path -LiteralPath $executable)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName '{AUTOSTART_TASK_NAME}' -TaskPath '\\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
""")


def _autostart_task_exists() -> bool:
    result = subprocess.run(
        _powershell_args(f"""
$task = Get-ScheduledTask -TaskName '{AUTOSTART_TASK_NAME}' -TaskPath '\\' -ErrorAction SilentlyContinue
if ($task -and $task.Settings.Enabled) {{ exit 0 }}
exit 1
"""),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def _run_autostart_command(args: list[str]) -> None:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        creationflags=CREATE_NO_WINDOW,
        check=False,
        timeout=30,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"自启任务设置失败，退出码 {result.returncode}")


def _remove_legacy_run_value() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def create_desktop_shortcut() -> None:
    # Only the packaged exe has a stable target; a source checkout has nothing
    # worth pinning to the desktop.
    if not getattr(sys, "frozen", False):
        return
    executable = Path(sys.executable)
    exe_literal = str(executable).replace("'", "''")
    workdir_literal = str(executable.parent).replace("'", "''")
    _run_autostart_command(_powershell_args(f"""
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) '{SHORTCUT_NAME}'
if (Test-Path -LiteralPath $lnk) {{ return }}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = '{exe_literal}'
$shortcut.Arguments = 'run'
$shortcut.WorkingDirectory = '{workdir_literal}'
$shortcut.IconLocation = '{exe_literal},0'
$shortcut.Description = '墨笺 InkMemo 后台同步'
$shortcut.Save()
"""))


def set_autostart(enabled: bool) -> None:
    if os.name != "nt":
        raise RuntimeError("登录自启只支持 Windows")
    if enabled:
        _run_autostart_command(build_autostart_create_args())
    else:
        _run_autostart_command(_powershell_args(f"""
Get-ScheduledTask -TaskName '{AUTOSTART_TASK_NAME}' -TaskPath '\\' -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false
"""))
    _remove_legacy_run_value()


def read_autostart() -> bool:
    if os.name != "nt":
        return False
    if _autostart_task_exists():
        return True
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except FileNotFoundError:
        return False
