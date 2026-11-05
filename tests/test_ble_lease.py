import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from inkmemo.ble import lease
from inkmemo.ble import client as ble


def test_lease_acquires_heartbeats_and_releases(monkeypatch):
    async def run():
        calls = []

        async def fake_request(payload):
            calls.append(dict(payload))
            if payload["op"] == "acquire":
                return {
                    "ok": True,
                    "state": "granted",
                    "token": "lease-token",
                    "expires_in": 120,
                }
            return {"ok": True, "state": "released"}

        monkeypatch.setattr(lease, "_request", fake_request)
        monkeypatch.setattr(lease, "HEARTBEAT_SECONDS", 0.01)
        async with lease.acquire_ble_lease("inkmemo") as session:
            assert session.coordinated is True
            assert session.token == "lease-token"
            await asyncio.sleep(0.03)

        assert [call["op"] for call in calls[:2]] == ["acquire", "heartbeat"]
        assert calls[-1]["op"] == "release"
        assert calls[-1]["token"] == "lease-token"

    asyncio.run(run())


def test_busy_lease_blocks_ble_operations(monkeypatch):
    async def run():
        async def fake_request(_payload):
            return {"ok": False, "state": "busy", "owner": "other"}

        monkeypatch.setattr(lease, "_request", fake_request)
        with pytest.raises(lease.BleLeaseBusy):
            async with lease.acquire_ble_lease("inkmemo"):
                raise AssertionError("busy lease must not yield")

    asyncio.run(run())


def test_coordinator_unavailable_keeps_standalone_operation(monkeypatch):
    async def run():
        async def unavailable(_payload):
            raise lease.BleLeaseUnavailable("no coordinator")

        monkeypatch.setattr(lease, "_request", unavailable)
        async with lease.acquire_ble_lease("inkmemo") as session:
            assert session.coordinated is False

    asyncio.run(run())


def test_release_runs_when_ble_operation_raises(monkeypatch):
    async def run():
        calls = []

        async def fake_request(payload):
            calls.append(payload["op"])
            if payload["op"] == "acquire":
                return {"ok": True, "state": "granted", "token": "lease-token"}
            return {"ok": True, "state": "released"}

        monkeypatch.setattr(lease, "_request", fake_request)
        with pytest.raises(RuntimeError):
            async with lease.acquire_ble_lease("inkmemo"):
                raise RuntimeError("ble failed")

        assert calls == ["acquire", "release"]

    asyncio.run(run())


def test_transfer_retries_hold_lease_around_every_attempt(monkeypatch):
    events = []

    @asynccontextmanager
    async def fake_lease(_owner):
        events.append("acquire")
        try:
            yield lease.LeaseSession(True, "lease-token", asyncio.Event())
        finally:
            events.append("release")

    async def fail_connect(*_args, **_kwargs):
        events.append("connect")
        raise TimeoutError()

    async def no_sleep(seconds):
        events.append(f"sleep:{seconds}")

    monkeypatch.setattr(ble, "acquire_ble_lease", fake_lease, raising=False)
    monkeypatch.setattr(ble.EpdConnection, "connect", staticmethod(fail_connect))
    monkeypatch.setattr(ble.asyncio, "sleep", no_sleep)

    with pytest.raises(ble.BleTransferError):
        asyncio.run(ble.transfer_with_retries("NRF_TEST", lambda _info: None))

    assert events[0] == "acquire"
    assert events[-1] == "release"
    assert events.count("connect") == 5
    assert events.count("sleep:5.0") == 1
    assert events.count("sleep:10.0") == 1
    assert events.count("sleep:15.0") == 1
    assert events.count("sleep:20.0") == 1


def test_scan_command_holds_lease(monkeypatch, capsys):
    from inkmemo import __main__ as cli
    import bleak

    events = []

    @asynccontextmanager
    async def fake_lease(_owner):
        events.append("acquire")
        try:
            yield lease.LeaseSession(True, "lease-token", asyncio.Event())
        finally:
            events.append("release")

    async def discover(**_kwargs):
        events.append("scan")
        return {}

    monkeypatch.setattr(cli, "acquire_ble_lease", fake_lease, raising=False)
    monkeypatch.setattr(bleak.BleakScanner, "discover", staticmethod(discover))

    assert asyncio.run(cli._scan()) == 0
    assert events == ["acquire", "scan", "release"]
    assert json.loads(capsys.readouterr().out) == []


def test_scan_command_reports_busy(monkeypatch, capsys):
    from inkmemo import __main__ as cli

    @asynccontextmanager
    async def busy(_owner):
        raise lease.BleLeaseBusy("蓝牙正由 LCD 占用")
        yield

    monkeypatch.setattr(cli, "acquire_ble_lease", busy)

    assert asyncio.run(cli._scan()) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["state"] == "busy"


def test_acquire_waits_longer_for_lcd_worker_cleanup(monkeypatch):
    timeouts = []

    async def fake_wait_for(awaitable, timeout):
        timeouts.append(timeout)
        awaitable.close()
        return {}

    monkeypatch.setattr(lease.asyncio, "wait_for", fake_wait_for)

    asyncio.run(lease._request({"op": "acquire"}))
    asyncio.run(lease._request({"op": "heartbeat"}))

    assert timeouts == [lease.ACQUIRE_TIMEOUT_SECONDS, lease.REQUEST_TIMEOUT_SECONDS]
