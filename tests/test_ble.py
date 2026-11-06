import asyncio
import time

import pytest

from inkmemo.ble.client import (
    BleTransferError,
    EpdConnection,
    EpdInfo,
    TransferAborted,
    parse_config,
)
from inkmemo.ble.protocol import (
    CMD_CLEAR,
    CMD_INIT,
    CMD_REFRESH,
    CMD_RESET_TRANSFER,
    CMD_SET_MODE,
    CMD_WRITE_BLOCK,
    MODE_CLOCK,
)
from inkmemo.ble.protocol import parse_transfer_status
from inkmemo.models import LayoutResult


def test_send_frame_aborts_before_touching_the_device():
    connection = EpdConnection(object())
    connection.info = EpdInfo(version=0x20, mtu=236, voltage_mv=None)
    connection.payload_size = 236
    frame = LayoutResult(
        layout="standard",
        png=b"",
        black=bytes(48_000),
        red=bytes(48_000),
        display_hash="test",
    )

    with pytest.raises(TransferAborted):
        asyncio.run(connection.send_frame(frame, should_abort=lambda: True))


def test_initialize_never_sends_init_because_it_wedges_the_command_queue():
    class FakeClient:
        def __init__(self):
            self.writes = []

        async def read_gatt_char(self, _uuid):
            return bytes([0x20])

        async def write_gatt_char(self, _uuid, packet, response=False):
            self.writes.append(bytes(packet))

    connection = EpdConnection(FakeClient())

    info = asyncio.run(connection.initialize())

    assert info.version == 0x20
    assert info.voltage_mv is None
    assert connection.client.writes == []


def test_config_notification_exposes_sleep_relevant_fields():
    payload = bytes([1, 2, 3, 4, 5, 6, 7, 0x61, 0xFF, 9, 10, 0, 1, 0])
    config = parse_config(payload)
    assert config is not None
    assert (config.model_id, config.wakeup_pin, config.display_mode) == (0x61, 0xFF, 0)
    assert parse_config(b"mtu=236") is None
    assert parse_config(b"t=1762000000") is None


def test_transfer_status_response_matches_firmware_layout():
    assert parse_transfer_status(bytes([0xA1, 3, 0, 3, 0, 7, 0])) == (3, 3, False)
    assert parse_transfer_status(bytes([0xA1, 3, 0, 2, 0, 7, 1])) == (3, 2, True)
    assert parse_transfer_status(bytes([0xA0, 0, 0, 0])) is None
    assert parse_transfer_status(bytes([0xA1, 0, 0])) is None


def test_each_layer_resets_transfer_before_blocks():
    class FakeClient:
        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _uuid, packet, response=False):
            self.writes.append(packet)
            if packet[0] == CMD_WRITE_BLOCK:
                connection._notifications.put_nowait(bytes([0xA0, 0, 0, 0]))

    connection = EpdConnection(FakeClient())
    connection.payload_size = 20

    asyncio.run(connection._send_layer(0x0F, bytes(20)))

    assert connection.client.writes[0] == bytes([CMD_RESET_TRANSFER])
    assert connection.client.writes[1][0] == CMD_WRITE_BLOCK


def test_frame_sends_blocks_then_refresh_without_panel_poking():
    class FakeClient:
        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _uuid, packet, response=False):
            packet = bytes(packet)
            self.writes.append(packet)
            if packet[0] == CMD_WRITE_BLOCK:
                block_id = packet[1] | (packet[2] << 8)
                connection._notifications.put_nowait(
                    bytes([0xA0, block_id & 0xFF, block_id >> 8, 0])
                )
            elif packet[0] == 0x32:
                connection._notifications.put_nowait(bytes([0xA1, 2, 0, 2, 0, 0, 0]))
            elif packet[0] == CMD_REFRESH:
                connection.disconnected.set()

    connection = EpdConnection(FakeClient())
    connection.info = EpdInfo(version=0x20, mtu=236, voltage_mv=None)
    connection.payload_size = 48_000
    frame = LayoutResult(
        layout="standard",
        png=b"",
        black=bytes(48_000),
        red=bytes(48_000),
        display_hash="test",
    )

    asyncio.run(connection.send_frame(frame, refresh_wait=0.2, panel_reset_wait=0))

    writes = connection.client.writes
    assert writes[0] == bytes([CMD_RESET_TRANSFER])
    assert writes[-1] == bytes([CMD_REFRESH])
    assert not any(w[0] in (0x03, 0x04) for w in writes)


def test_panel_is_reset_via_clock_mode_before_the_first_block():
    class FakeClient:
        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _uuid, packet, response=False):
            packet = bytes(packet)
            self.writes.append(packet)
            if packet[0] == CMD_WRITE_BLOCK:
                block_id = packet[1] | (packet[2] << 8)
                connection._notifications.put_nowait(
                    bytes([0xA0, block_id & 0xFF, block_id >> 8, 0])
                )
            elif packet[0] == 0x32:
                connection._notifications.put_nowait(bytes([0xA1, 2, 0, 2, 0, 0, 0]))
            elif packet[0] == CMD_REFRESH:
                connection.disconnected.set()

    connection = EpdConnection(FakeClient())
    connection.info = EpdInfo(version=0x20, mtu=236, voltage_mv=None)
    connection.payload_size = 48_000
    frame = LayoutResult(
        layout="relaxed",
        png=b"",
        black=bytes(48_000),
        red=bytes(48_000),
        display_hash="test",
    )

    asyncio.run(
        connection.send_frame(
            frame, refresh_wait=0.1, panel_reset_wait=0.05, panel_clear_wait=0
        )
    )

    writes = connection.client.writes
    assert writes[0] == bytes([CMD_SET_MODE, MODE_CLOCK])
    assert next(i for i, w in enumerate(writes) if w[0] == CMD_WRITE_BLOCK) > 0


def test_panel_is_cleared_to_white_before_the_first_block():
    class FakeClient:
        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, _uuid, packet, response=False):
            packet = bytes(packet)
            self.writes.append(packet)
            if packet[0] == CMD_WRITE_BLOCK:
                block_id = packet[1] | (packet[2] << 8)
                connection._notifications.put_nowait(
                    bytes([0xA0, block_id & 0xFF, block_id >> 8, 0])
                )
            elif packet[0] == 0x32:
                connection._notifications.put_nowait(bytes([0xA1, 2, 0, 2, 0, 0, 0]))
            elif packet[0] == CMD_REFRESH:
                connection.disconnected.set()

    connection = EpdConnection(FakeClient())
    connection.info = EpdInfo(version=0x20, mtu=236, voltage_mv=None)
    connection.payload_size = 48_000
    frame = LayoutResult(
        layout="relaxed",
        png=b"",
        black=bytes(48_000),
        red=bytes(48_000),
        display_hash="test",
    )

    asyncio.run(
        connection.send_frame(
            frame, refresh_wait=0.1, panel_reset_wait=0.05, panel_clear_wait=0.05
        )
    )

    writes = connection.client.writes
    assert writes[0] == bytes([CMD_SET_MODE, MODE_CLOCK])
    assert writes[1] == bytes([CMD_CLEAR])
    assert writes[2] == bytes([CMD_RESET_TRANSFER])


def test_disconnect_during_refresh_is_uncertain():
    class FakeClient:
        refresh_response = None

        async def write_gatt_char(self, _uuid, packet, response=False):
            if packet[0] == CMD_WRITE_BLOCK:
                block_id = packet[1] | (packet[2] << 8)
                connection._notifications.put_nowait(bytes([0xA0, block_id & 0xFF, block_id >> 8, 0]))
            elif packet[0] == 0x32:
                connection._notifications.put_nowait(bytes([0xA1, 2, 0, 2, 0, 0, 0]))
            elif packet[0] == CMD_REFRESH:
                self.refresh_response = response
                connection.disconnected.set()

    connection = EpdConnection(FakeClient())
    connection.info = EpdInfo(version=0x20, mtu=236, voltage_mv=None)
    connection.payload_size = 248
    frame = LayoutResult(
        layout="standard",
        png=b"",
        black=bytes(48_000),
        red=bytes(48_000),
        display_hash="test",
    )

    started = time.monotonic()
    requested, uncertain = asyncio.run(
        connection.send_frame(frame, refresh_wait=1, panel_reset_wait=0)
    )

    assert requested is True
    assert uncertain is True
    assert connection.client.refresh_response is True
    assert time.monotonic() - started >= 0.9


def test_transfer_retries_wait_longer_between_attempts(monkeypatch):
    from inkmemo.ble import client as ble

    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    async def fail_connect(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(ble.EpdConnection, "connect", staticmethod(fail_connect))
    monkeypatch.setattr(ble.asyncio, "sleep", fake_sleep)

    with pytest.raises(ble.BleTransferError):
        asyncio.run(ble.transfer_with_retries("NRF_TEST", lambda _info: None))

    assert delays == [5.0, 10.0, 15.0, 20.0]


def test_missing_device_keeps_its_own_error(monkeypatch):
    from inkmemo.ble import client as ble

    async def no_sleep(_seconds):
        return None

    async def missing(*_args, **_kwargs):
        raise ble.DeviceNotFound("未发现目标设备")

    monkeypatch.setattr(ble.EpdConnection, "connect", staticmethod(missing))
    monkeypatch.setattr(ble.asyncio, "sleep", no_sleep)

    with pytest.raises(ble.DeviceNotFound):
        asyncio.run(ble.transfer_with_retries("NRF_MISSING", lambda _info: None))


def test_connect_timeout_is_its_own_error(monkeypatch):
    from inkmemo.ble import client as ble

    async def no_sleep(_seconds):
        return None

    async def hang(*_args, **_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(ble.EpdConnection, "connect", staticmethod(hang))
    monkeypatch.setattr(ble.asyncio, "sleep", no_sleep)

    with pytest.raises(ble.BleConnectTimeout):
        asyncio.run(ble.transfer_with_retries("NRF_TEST", lambda _info: None))


def test_hung_gatt_connect_is_bounded_and_released(monkeypatch):
    from inkmemo.ble import client as ble

    released = {}
    sessions = []

    class Session:
        maintain_connection = True

        def close(self):
            released["session"] = True

    class Backend:
        def __init__(self):
            self._session = Session()
            self._requester = None
            sessions.append(self._session)

    class HangingClient:
        def __init__(self, *_args, **_kwargs):
            self._backend = Backend()

        async def connect(self):
            await asyncio.sleep(30)

        async def disconnect(self):
            released["disconnect"] = True

    async def fake_find(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(ble.BleakScanner, "find_device_by_name", staticmethod(fake_find))
    monkeypatch.setattr(ble, "BleakClient", HangingClient)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(ble.EpdConnection.connect("NRF_TEST", timeout=0.3))

    assert time.monotonic() - started < 5
    assert released == {"disconnect": True, "session": True}
    assert sessions[0].maintain_connection is False


def test_slow_write_times_out_instead_of_hanging():
    class Client:
        async def write_gatt_char(self, *_args, **_kwargs):
            await asyncio.sleep(30)

    connection = EpdConnection(Client())

    started = time.monotonic()
    with pytest.raises(BleTransferError):
        asyncio.run(connection._write(b"\x00", timeout=0.2))

    assert time.monotonic() - started < 3


def test_failed_connect_releases_winrt_session():
    from inkmemo.ble import client as ble

    class Session:
        maintain_connection = True
        closed = False

        def close(self):
            self.closed = True

    class Requester:
        closed = False

        def close(self):
            self.closed = True

    class Backend:
        def __init__(self):
            self._session = Session()
            self._requester = Requester()

    class Client:
        def __init__(self):
            self._backend = Backend()

    client = Client()
    ble._release_winrt_client(client)

    assert client._backend._session.maintain_connection is False
    assert client._backend._session.closed is True
    assert client._backend._requester.closed is True
