from __future__ import annotations

import math


CMD_INIT = 0x01
CMD_CLEAR = 0x02
CMD_REFRESH = 0x05
CMD_SEND_COMMAND = 0x03
CMD_SEND_DATA = 0x04
CMD_SET_MODE = 0x22
CMD_SYS_RESET = 0x91
CMD_WRITE_BLOCK = 0x31
CMD_QUERY_STATUS = 0x32
CMD_RESET_TRANSFER = 0x33

MODE_CLOCK = 0x02

SERVICE_UUID = "62750001-d828-918d-fb46-b6c11c675aec"
COMMAND_UUID = "62750002-d828-918d-fb46-b6c11c675aec"
VERSION_UUID = "62750003-d828-918d-fb46-b6c11c675aec"

LAYER_BLACK = 0x0F
LAYER_RED = 0x00

REG_PTIN = 0x91
REG_PTOUT = 0x92
REG_PTL = 0x90
REG_PSR = 0x00
REG_CDI = 0x50
# Full 800x480 window in PTL form: x0, x1, y0, y1, scan mode.
FULL_WINDOW = bytes((0x00, 0x00, 0x03, 0x1F, 0x00, 0x00, 0x01, 0xDF, 0x00))
# BWR panel setup normally written by the firmware's UC8179_Init.
PANEL_SETUP = bytes((0x0F,))
PANEL_CDI = bytes((0x77,))

MAX_BLOCKS = 512
PLANE_BYTES = 48_000


class ProtocolError(RuntimeError):
    pass


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc >> 1) ^ 0x8408) if crc & 1 else crc >> 1
    return crc & 0xFFFF


def block_count(data_length: int, payload_size: int) -> int:
    return math.ceil(data_length / payload_size)


def build_block(block_id: int, total_blocks: int, layer: int, payload: bytes) -> bytes:
    if not 0 <= block_id < total_blocks <= MAX_BLOCKS:
        raise ProtocolError("block 参数越界")
    if layer not in (LAYER_BLACK, LAYER_RED):
        raise ProtocolError("非法图层")
    cfg = layer if block_id == 0 else 0xF0 | layer
    checksum = crc16(payload)
    packet = bytearray(8 + len(payload))
    packet[0] = CMD_WRITE_BLOCK
    packet[1] = block_id & 0xFF
    packet[2] = block_id >> 8
    packet[3] = total_blocks & 0xFF
    packet[4] = total_blocks >> 8
    packet[5] = cfg
    packet[6 : 6 + len(payload)] = payload
    packet[6 + len(payload)] = checksum & 0xFF
    packet[7 + len(payload)] = checksum >> 8
    return bytes(packet)


def parse_ack(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 4 and data[0] == 0xA0:
        return data[1] | (data[2] << 8), data[3]
    return None


def parse_transfer_status(data: bytes) -> tuple[int, int, bool] | None:
    if len(data) < 7 or data[0] != 0xA1:
        return None
    return (
        data[1] | (data[2] << 8),
        data[3] | (data[4] << 8),
        data[6] == 1,
    )
