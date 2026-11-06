from inkmemo.ble.protocol import (
    LAYER_BLACK,
    LAYER_RED,
    block_count,
    build_block,
    crc16,
    parse_ack,
)


def test_crc_known_vector():
    assert crc16(b"123456789") == 0x6F91
    assert crc16(b"\x00") == 0x0F87


def test_packet_shape_and_crc_scope():
    payload = bytes(range(40))
    packet = build_block(0, 204, LAYER_BLACK, payload)
    assert packet[0] == 0x31
    assert packet[1:3] == b"\x00\x00"
    assert packet[3:5] == b"\xcc\x00"
    assert packet[5] == 0x0F
    assert packet[6:-2] == payload
    assert packet[-2:] == crc16(payload).to_bytes(2, "little")
    assert build_block(1, 204, LAYER_BLACK, payload)[5] == 0xFF
    assert build_block(0, 204, LAYER_RED, payload)[5] == 0x00


def test_204_blocks_at_236_payload():
    assert block_count(48_000, 236) == 204
    assert block_count(48_000, 20) == 2400


def test_ack_parser():
    assert parse_ack(bytes([0xA0, 0x2C, 0x01, 0])) == (300, 0)
    assert parse_ack(bytes([0xA0, 0x01, 0x00, 1])) == (1, 1)
    assert parse_ack(b"mtu=244") is None
