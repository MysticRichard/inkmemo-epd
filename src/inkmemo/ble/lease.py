from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from multiprocessing.connection import Client
from pathlib import Path

PROTOCOL_VERSION = 1
PIPE_NAME = r"\\.\pipe\MagpieLcdBridge.BleLease.v1"
KEY_PATH = (
    Path(os.environ.get("LOCALAPPDATA", Path.home()))
    / "MagpieLcdBridge"
    / "ble-lease.key"
)
DEFAULT_TTL_SECONDS = 120
HEARTBEAT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 4.0
# acquire waits for the LCD worker to stop; leave headroom for its bounded cleanup.
ACQUIRE_TIMEOUT_SECONDS = 30.0

_log = logging.getLogger("inkmemo.ble.lease")


class BleLeaseUnavailable(RuntimeError):
    pass


class BleLeaseBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseSession:
    coordinated: bool
    token: str | None = None
    lost: asyncio.Event | None = None

    def should_abort(self) -> bool:
        return self.lost is not None and self.lost.is_set()


def _exchange(payload: dict) -> dict:
    try:
        key = KEY_PATH.read_bytes()
    except OSError as error:
        raise BleLeaseUnavailable("LCD 蓝牙协调器未运行") from error
    request = {"protocol": PROTOCOL_VERSION, **payload}
    try:
        with Client(PIPE_NAME, family="AF_PIPE", authkey=key) as connection:
            connection.send_bytes(
                json.dumps(request, ensure_ascii=False).encode("utf-8")
            )
            raw = connection.recv_bytes(MAX_MESSAGE_BYTES)
    except OSError as error:
        raise BleLeaseUnavailable("无法连接 LCD 蓝牙协调器") from error
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise BleLeaseUnavailable("LCD 蓝牙协调器响应格式无效")
    return response


async def _request(payload: dict) -> dict:
    timeout = (
        ACQUIRE_TIMEOUT_SECONDS
        if payload.get("op") == "acquire"
        else REQUEST_TIMEOUT_SECONDS
    )
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_exchange, payload),
            timeout=timeout,
        )
    except asyncio.TimeoutError as error:
        raise BleLeaseUnavailable("LCD 蓝牙协调器响应超时") from error


async def _heartbeat(token: str, lost: asyncio.Event) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            response = await _request(
                {
                    "op": "heartbeat",
                    "token": token,
                    "ttl_seconds": DEFAULT_TTL_SECONDS,
                }
            )
        except Exception as error:
            _log.warning("ble lease heartbeat unavailable: %s", type(error).__name__)
            lost.set()
            return
        if not response.get("ok"):
            _log.warning("ble lease heartbeat rejected: %s", response.get("state"))
            lost.set()
            return


@asynccontextmanager
async def acquire_ble_lease(owner: str = "inkmemo"):
    try:
        response = await _request(
            {
                "op": "acquire",
                "owner": owner,
                "ttl_seconds": DEFAULT_TTL_SECONDS,
            }
        )
    except BleLeaseUnavailable as error:
        _log.warning("ble lease unavailable; continuing standalone: %s", error)
        yield LeaseSession(False)
        return

    if not response.get("ok"):
        if response.get("state") == "busy":
            raise BleLeaseBusy(
                f"蓝牙正由 {response.get('owner') or '其他程序'} 占用"
            )
        raise BleLeaseUnavailable(
            f"LCD 蓝牙协调器拒绝租约：{response.get('state')}"
        )

    token = response.get("token")
    if not isinstance(token, str) or not token:
        raise BleLeaseUnavailable("LCD 蓝牙协调器未返回有效 token")

    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(token, lost))
    try:
        yield LeaseSession(True, token, lost)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            response = await _request(
                {
                    "op": "release",
                    "owner": owner,
                    "token": token,
                }
            )
        except Exception as error:
            _log.warning("ble lease release unavailable: %s", type(error).__name__)
        else:
            if not response.get("ok"):
                _log.warning("ble lease release rejected: %s", response.get("state"))
