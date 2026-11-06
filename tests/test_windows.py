import base64
import json
import subprocess
import sys

import pytest

import inkmemo.__main__ as cli
import inkmemo.windows as windows
from inkmemo.windows import (
    SingleInstance,
    build_autostart_create_args,
    service_running,
)


def test_service_running_tracks_the_single_instance_mutex():
    if service_running():
        pytest.skip("InkMemo 后台正在运行，无法独占互斥体")
    instance = SingleInstance()
    assert instance.acquire() is True
    try:
        assert service_running() is True
    finally:
        instance.release()
    assert service_running() is False


def test_autostart_builds_an_interactive_user_task_without_registering(monkeypatch):
    executable = r"C:\Program Files\Ink'Memo $cache\InkMemo.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    command = build_autostart_create_args()
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    preview_registration = """
Import-Module ScheduledTasks -ErrorAction Stop
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Action, $Trigger, $Principal, $Settings, [switch]$Force)
    $definition = [pscustomobject]@{
        Name = $TaskName
        Execute = $Action.Execute
        Arguments = $Action.Arguments
        Directory = $Action.WorkingDirectory
        User = $Principal.UserId
        TriggerUser = $Trigger.UserId
        LogonType = [int]$Principal.LogonType
        RunLevel = [int]$Principal.RunLevel
        Delay = $Trigger.Delay
        Hidden = $Settings.Hidden
        MultipleInstances = [int]$Settings.MultipleInstances
        ExecutionTimeLimit = $Settings.ExecutionTimeLimit
    }
    [Console]::WriteLine(($definition | ConvertTo-Json -Compress))
}
"""
    encoded = base64.b64encode(
        (preview_registration + script).encode("utf-16-le")
    ).decode("ascii")
    result = subprocess.run(
        [*command[:-1], encoded], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert result.stdout, result.stderr
    definition = json.loads(result.stdout)

    assert definition["Name"] == "InkMemo"
    assert definition["Execute"] == executable
    assert definition["Arguments"] == "run"
    assert definition["Directory"] == r"C:\Program Files\Ink'Memo $cache"
    assert definition["User"] == definition["TriggerUser"]
    assert definition["LogonType"] == 3
    assert definition["RunLevel"] == 0
    assert definition["Delay"] == "PT30S"
    assert definition["Hidden"] is True
    assert definition["MultipleInstances"] == 2
    assert definition["ExecutionTimeLimit"] == "PT0S"


def test_failed_autostart_registration_preserves_legacy_run(monkeypatch):
    removed = []
    monkeypatch.setattr(
        windows.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "Access is denied"),
    )
    monkeypatch.setattr(windows, "_remove_legacy_run_value", lambda: removed.append(True))

    with pytest.raises(RuntimeError):
        windows.set_autostart(True)

    assert removed == []


def test_desktop_shortcut_is_skipped_when_running_from_source(monkeypatch):
    calls = []
    monkeypatch.setattr(windows, "_run_autostart_command", calls.append)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    windows.create_desktop_shortcut()

    assert calls == []


def test_desktop_shortcut_points_at_the_packaged_exe(monkeypatch):
    calls = []
    executable = r"C:\Program Files\Ink'Memo $cache\InkMemo.exe"
    monkeypatch.setattr(windows, "_run_autostart_command", calls.append)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", executable)

    windows.create_desktop_shortcut()

    script = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    assert "Desktop" in script
    assert "墨笺 InkMemo.lnk" in script
    assert r"C:\Program Files\Ink''Memo $cache\InkMemo.exe" in script
    assert r"C:\Program Files\Ink''Memo $cache" in script
    assert "$shortcut.Arguments = 'run'" in script
    assert "if (Test-Path -LiteralPath $lnk) { return }" in script


@pytest.mark.parametrize("frozen", [False, True])
def test_cli_autostart_failure_is_reported_without_crashing(monkeypatch, capsys, frozen):
    from tkinter import messagebox

    shown = []

    def deny(_enabled):
        raise RuntimeError("Access is denied")

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", ["inkmemo", "autostart", "on"])
    monkeypatch.setattr(cli, "set_autostart", deny)
    monkeypatch.setattr(messagebox, "showerror", lambda *args: shown.append(args))
    monkeypatch.setattr(cli.Settings, "load", lambda: pytest.fail("failed registration must not save"))

    assert cli.main() == 1
    if frozen:
        assert len(shown) == 1
        assert "Access is denied" in shown[0][1]
    else:
        error = json.loads(capsys.readouterr().out)
        assert error["ok"] is False
        assert error["stage"] == "autostart"


def test_cli_pause_keeps_runtime_state_in_sync(monkeypatch):
    saved = {}

    class FakeSettings:
        paused = False

        @classmethod
        def load(cls):
            return cls()

        def save(self):
            saved["paused"] = self.paused

    class FakeState:
        def update(self, **changes):
            saved.update(changes)

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(cli, "RuntimeState", FakeState)
    monkeypatch.setattr(sys, "argv", ["inkmemo", "pause"])

    assert cli.main() == 0
    assert saved == {"paused": True, "service_health": "paused"}

    saved.clear()
    FakeSettings.paused = True
    monkeypatch.setattr(sys, "argv", ["inkmemo", "pause", "--resume"])

    assert cli.main() == 0
    assert saved == {"paused": False, "service_health": "active"}


def test_cli_set_link_resets_previous_source_state(monkeypatch):
    calls = []

    class FakeStore:
        def set_url(self, value):
            calls.append(("set_url", value))

    monkeypatch.setattr(cli, "SecretStore", FakeStore)
    monkeypatch.setattr(cli, "reset_source_cache", lambda: calls.append(("reset",)))
    monkeypatch.setattr(sys, "argv", ["inkmemo", "set-link", "https://kdocs.cn/l/test"])

    assert cli.main() == 0
    assert calls == [
        ("set_url", "https://kdocs.cn/l/test"),
        ("reset",),
    ]
