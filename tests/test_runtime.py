import asyncio
import io
from datetime import datetime
from types import SimpleNamespace

from PIL import Image

from inkmemo import runtime
from inkmemo.ble.client import BleTransferError, EpdInfo, TransferResult
from inkmemo.models import Grid, LayoutResult, Weather
from inkmemo.render.layouts import LayoutOverflow
from inkmemo.runtime import SyncService
from inkmemo.sources.wps import WpsSourceError
from inkmemo.storage import SecretStore, Settings


def test_paused_check_returns_without_reading(tmp_path):
    service = SyncService(Settings(paused=True), base_dir=tmp_path)
    result = asyncio.run(service.check())
    assert result.stage == "paused"


def test_running_service_picks_up_pause_from_disk(tmp_path):
    service = SyncService(Settings(paused=False), base_dir=tmp_path)
    Settings(paused=True).save(tmp_path)

    service._reload_settings()

    assert service.settings.paused is True
    assert asyncio.run(service.check()).stage == "paused"


def test_next_wake_detects_external_settings_change(tmp_path):
    async def run():
        service = SyncService(Settings(), base_dir=tmp_path)
        waiter = asyncio.create_task(service._next_wake())
        await asyncio.sleep(0.05)
        Settings(paused=True).save(tmp_path)
        await asyncio.wait_for(waiter, timeout=3)
        assert service.force_event.is_set() is False

    asyncio.run(run())


def test_pause_wakes_running_service(tmp_path):
    service = SyncService(Settings(), base_dir=tmp_path)
    service.pause()
    assert service.force_event.is_set()


def test_preview_cloud_failure_never_touches_ble(tmp_path, monkeypatch):
    class FailingReader:
        async def read(self, _url):
            raise WpsSourceError("test")

    class ErrorRenderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="error",
                png=b"error",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="error",
            )

    def unexpected_ble(*_args, **_kwargs):
        raise AssertionError("preview must not connect to BLE")

    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(Settings(), base_dir=tmp_path)
    service.reader = FailingReader()
    service.renderer = ErrorRenderer()
    monkeypatch.setattr(runtime, "transfer_with_retries", unexpected_ble)

    result = asyncio.run(service.check(preview_only=True))

    assert result.stage == "source"


def test_unchanged_content_skips_ble(tmp_path, monkeypatch):
    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class Renderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="standard",
                png=b"png",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="same",
            )

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    def unexpected_ble(*_args, **_kwargs):
        raise AssertionError("unchanged content must not touch BLE")

    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(Settings(), base_dir=tmp_path)
    service.state.update(last_sent_display_hash="same")
    service.reader = Reader()
    service.renderer = Renderer()
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", unexpected_ble)

    result = asyncio.run(service.check())

    assert result.stage == "unchanged"


def test_overflow_skips_ble_and_keeps_previous_frame(tmp_path, monkeypatch):
    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class OverflowRenderer:
        async def render(self, *_args, **_kwargs):
            raise LayoutOverflow("LAYOUT_OVERFLOW：事项超过 30 项")

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    def unexpected_ble(*_args, **_kwargs):
        raise AssertionError("overflow must not touch BLE")

    notices = []

    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(
        Settings(), base_dir=tmp_path, notify=lambda title, message: notices.append(title)
    )
    service.reader = Reader()
    service.renderer = OverflowRenderer()
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", unexpected_ble)

    result = asyncio.run(service.check())

    assert result.stage == "render"
    assert service.state.read()["last_error_class"] == "layout_overflow"
    assert notices == ["版面放不下"]


def test_missing_board_is_classified_and_keeps_previous_frame(tmp_path, monkeypatch):
    from inkmemo.ble.client import DeviceNotFound

    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class Renderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="standard",
                png=b"png",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="frame",
            )

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    async def missing_board(*_args, **_kwargs):
        raise DeviceNotFound("未发现目标设备")

    notices = []
    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(
        Settings(), base_dir=tmp_path, notify=lambda title, message: notices.append(title)
    )
    service.reader = Reader()
    service.renderer = Renderer()
    service.state.update(last_error_class="auth")
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", missing_board)

    result = asyncio.run(service.check())

    state = service.state.read()
    assert result.stage == "ble"
    assert state["ble_health"] == "not_found"
    assert state["display_health"] == "kept_previous"
    assert state["last_error_class"] == "ble_not_found"
    assert notices == ["未发现看板"]


def test_ble_failure_is_notified_and_keeps_previous_frame(tmp_path, monkeypatch):
    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class Renderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="standard",
                png=b"png",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="frame",
            )

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    async def failing_board(*_args, **_kwargs):
        raise BleTransferError("test")

    notices = []
    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(
        Settings(), base_dir=tmp_path, notify=lambda title, message: notices.append(title)
    )
    service.reader = Reader()
    service.renderer = Renderer()
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", failing_board)

    result = asyncio.run(service.check())

    state = service.state.read()
    assert result.stage == "ble"
    assert state["ble_health"] == "failed"
    assert state["display_health"] == "kept_previous"
    assert state["last_error_class"] == "ble_failed"
    assert notices == ["蓝牙同步失败"]


def test_connect_timeout_tells_user_to_restart_bluetooth(tmp_path, monkeypatch):
    from inkmemo.ble.client import BleConnectTimeout

    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class Renderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="standard",
                png=b"png",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="frame",
            )

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    async def timeout(*_args, **_kwargs):
        raise BleConnectTimeout("连接看板超时")

    messages: list[tuple[str, str]] = []
    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(
        Settings(),
        base_dir=tmp_path,
        notify=lambda title, message: messages.append((title, message)),
    )
    service.reader = Reader()
    service.renderer = Renderer()
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", timeout)

    result = asyncio.run(service.check())

    state = service.state.read()
    assert result.stage == "ble"
    assert state["ble_health"] == "connect_timeout"
    assert state["display_health"] == "kept_previous"
    assert state["last_error_class"] == "ble_connect_timeout"
    assert messages[0][0] == "蓝牙连接超时"
    assert "重启" in messages[0][1] or "关闭再开启蓝牙" in messages[0][1]


def test_busy_lcd_lease_is_reported_separately(tmp_path, monkeypatch):
    from inkmemo.ble.lease import BleLeaseBusy

    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class Renderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="standard",
                png=b"png",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="frame",
            )

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    async def busy(*_args, **_kwargs):
        raise BleLeaseBusy("蓝牙正由 inkmemo 占用")

    messages: list[tuple[str, str]] = []
    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(
        Settings(),
        base_dir=tmp_path,
        notify=lambda title, message: messages.append((title, message)),
    )
    service.reader = Reader()
    service.renderer = Renderer()
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", busy)

    result = asyncio.run(service.check())

    state = service.state.read()
    assert result.stage == "ble"
    assert state["ble_health"] == "busy"
    assert state["last_error_class"] == "ble_busy"
    assert messages[-1][0] == "蓝牙被占用"


def test_cached_failure_prefers_last_sent_frame(tmp_path, monkeypatch):
    previews = tmp_path / "previews"
    previews.mkdir()

    def solid_png(color):
        buffer = io.BytesIO()
        Image.new("RGB", (800, 480), color).save(buffer, format="PNG")
        return buffer.getvalue()

    (previews / "last_sent.png").write_bytes(solid_png("red"))
    (previews / "last_good.png").write_bytes(solid_png("blue"))
    captured = {}

    async def fake_transfer(_device, factory, **_kwargs):
        captured["frame"] = await factory(None)
        raise BleTransferError("stop after capture")

    monkeypatch.setattr(runtime, "transfer_with_retries", fake_transfer)
    service = SyncService(Settings(), base_dir=tmp_path)
    service.state.update(last_good_at=datetime(2026, 11, 3, 10, 0).isoformat())

    asyncio.run(service._source_failed("cloud", "测试", True))

    image = Image.open(io.BytesIO(captured["frame"].png)).convert("RGB")
    assert image.getpixel((0, 0)) == (255, 0, 0)


def test_cached_failure_stamp_uses_screen_time(tmp_path, monkeypatch):
    previews = tmp_path / "previews"
    previews.mkdir()
    buffer = io.BytesIO()
    Image.new("RGB", (800, 480), "white").save(buffer, format="PNG")
    (previews / "last_sent.png").write_bytes(buffer.getvalue())

    seen = {}

    def fake_overlay(png, message, fetched_at):
        seen["at"] = fetched_at
        return LayoutResult(
            layout="cached-error",
            png=png,
            black=bytes(48_000),
            red=bytes(48_000),
            display_hash="cached",
        )

    async def failing_transfer(*_args, **_kwargs):
        raise BleTransferError("stop after overlay")

    monkeypatch.setattr(runtime, "overlay_failure_footer", fake_overlay)
    monkeypatch.setattr(runtime, "transfer_with_retries", failing_transfer)
    service = SyncService(Settings(), base_dir=tmp_path)
    notices = []
    service.notify = lambda title, message: notices.append(title)
    service.state.update(
        last_good_at="2026-11-03T13:24:12+08:00",
        last_transfer_confirmed_at="2026-11-03T12:29:12+08:00",
    )

    result = asyncio.run(service._source_failed("cloud", "测试", True))

    assert seen["at"] == datetime.fromisoformat("2026-11-03T12:29:12+08:00")
    assert result.stage == "ble"
    assert service.state.read()["display_health"] == "kept_previous"
    assert service.state.read()["last_error_class"] == "ble_failed"
    assert notices == ["WPS 读取失败", "蓝牙同步失败"]


def test_cached_failure_keeps_refresh_uncertain(tmp_path, monkeypatch):
    previews = tmp_path / "previews"
    previews.mkdir()
    buffer = io.BytesIO()
    Image.new("RGB", (800, 480), "red").save(buffer, format="PNG")
    (previews / "last_sent.png").write_bytes(buffer.getvalue())

    async def fake_transfer(_device, factory, **_kwargs):
        frame = await factory(None)
        return TransferResult(
            info=EpdInfo(version=0x20, mtu=236, voltage_mv=None),
            layout=frame,
            refresh_requested=True,
            refresh_uncertain=True,
        )

    monkeypatch.setattr(runtime, "transfer_with_retries", fake_transfer)
    service = SyncService(Settings(), base_dir=tmp_path)
    service.state.update(last_good_at=datetime(2026, 11, 3, 10, 0).isoformat())

    result = asyncio.run(service._source_failed("cloud", "测试", True))

    assert result.stage == "cached"
    assert service.state.read()["display_health"] == "refresh_uncertain"


def test_cached_failure_connect_timeout_asks_for_bluetooth_restart(tmp_path, monkeypatch):
    from inkmemo.ble.client import BleConnectTimeout

    previews = tmp_path / "previews"
    previews.mkdir()
    buffer = io.BytesIO()
    Image.new("RGB", (800, 480), "white").save(buffer, format="PNG")
    (previews / "last_sent.png").write_bytes(buffer.getvalue())

    async def timeout(*_args, **_kwargs):
        raise BleConnectTimeout("连接看板超时")

    monkeypatch.setattr(runtime, "transfer_with_retries", timeout)
    service = SyncService(Settings(), base_dir=tmp_path)
    messages = []
    service.notify = lambda title, message: messages.append((title, message))
    service.state.update(last_transfer_confirmed_at="2026-11-03T12:29:12+08:00")

    result = asyncio.run(service._source_failed("cloud", "测试", True))

    assert result.stage == "ble"
    assert service.state.read()["last_error_class"] == "ble_connect_timeout"
    assert service.state.read()["display_health"] == "kept_previous"
    assert messages[-1][0] == "蓝牙连接超时"
    assert "关闭再开启蓝牙" in messages[-1][1]


def test_successful_send_writes_last_sent_frame(tmp_path, monkeypatch):
    class Reader:
        async def read(self, _url):
            return Grid(
                values=[
                    ["产品", "事项", "完成时间", "责任人", "对接人"],
                    ["项目", "事项", "11.03", "甲", "乙"],
                ],
                merges=[],
                document_title="",
                sheet_name="",
                fetched_at=datetime(2026, 11, 3, 10, 0),
            )

    class Renderer:
        async def render(self, *_args, **_kwargs):
            return LayoutResult(
                layout="standard",
                png=b"frame",
                black=bytes(48_000),
                red=bytes(48_000),
                display_hash="same",
            )

    async def location(*_args, **_kwargs):
        return SimpleNamespace(city="测试", latitude=0.0, longitude=0.0)

    async def fake_transfer(_device, factory, **_kwargs):
        frame = await factory(EpdInfo(version=0x20, mtu=236, voltage_mv=None))
        return TransferResult(
            info=EpdInfo(version=0x20, mtu=236, voltage_mv=None),
            layout=frame,
            refresh_requested=True,
            refresh_uncertain=True,
        )

    SecretStore(tmp_path).set_url("https://kdocs.cn/l/fake")
    service = SyncService(Settings(), base_dir=tmp_path)
    service.reader = Reader()
    service.renderer = Renderer()
    monkeypatch.setattr(runtime, "resolve_weather_location", location)
    monkeypatch.setattr(
        runtime,
        "fetch_weather",
        lambda *_args, **_kwargs: Weather("测试", "晴", "sun", 20, 15, 22),
    )
    monkeypatch.setattr(runtime, "transfer_with_retries", fake_transfer)

    result = asyncio.run(service.check(force_send=True))

    assert result.stage == "display"
    assert (tmp_path / "previews" / "last_sent.png").read_bytes() == b"frame"
    assert service.state.read()["display_health"] == "refresh_uncertain"




def test_run_forever_survives_a_transient_state_error(tmp_path, monkeypatch):
    service = SyncService(Settings(paused=True), base_dir=tmp_path)
    monkeypatch.setattr(runtime, "LOOP_ERROR_DELAY", 0.01)
    writes = {"count": 0}
    real_update = service.state.update

    def flaky(**changes):
        writes["count"] += 1
        if writes["count"] == 1:
            raise OSError("disk busy")
        return real_update(**changes)

    service.state.update = flaky

    async def run():
        task = asyncio.create_task(service.run_forever())
        await asyncio.sleep(0.2)
        service.stop_event.set()
        service.force_event.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(run())

    assert writes["count"] > 1
