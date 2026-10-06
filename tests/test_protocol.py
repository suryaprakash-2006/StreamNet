"""
Unit tests for StreamNet Protocol Framing and Serialization.
"""

import pytest
from common.protocol import (
    MAGIC,
    VERSION,
    TCP_HEADER_SIZE,
    UDP_HEADER_SIZE,
    HeaderFlags,
    MsgType,
    TCPHeader,
    UDPHeader,
)


def test_tcp_header_pack_unpack():
    """Verify standard TCPHeader packing and unpacking."""
    header = TCPHeader(
        msg_type=MsgType.DATA_FRAME,
        flags=HeaderFlags.KEYFRAME,
        frame_id=42,
        timestamp_ns=1_700_000_000_123_456,
        payload_len=65536,
    )
    packed = header.pack()
    assert len(packed) == TCP_HEADER_SIZE
    assert packed[:2] == MAGIC

    unpacked = TCPHeader.unpack(packed)
    assert unpacked.version == VERSION
    assert unpacked.msg_type == MsgType.DATA_FRAME
    assert unpacked.flags == HeaderFlags.KEYFRAME
    assert unpacked.frame_id == 42
    assert unpacked.timestamp_ns == 1_700_000_000_123_456
    assert unpacked.payload_len == 65536


def test_tcp_header_invalid_magic():
    """Verify exception when magic bytes do not match."""
    header = TCPHeader(
        msg_type=MsgType.DATA_FRAME,
        flags=HeaderFlags.NONE,
        frame_id=1,
        timestamp_ns=1000,
        payload_len=100,
    )
    packed = bytearray(header.pack())
    packed[0] = ord("X")  # Corrupt magic byte

    with pytest.raises(ValueError, match="Invalid magic bytes"):
        TCPHeader.unpack(bytes(packed))


def test_tcp_header_short_buffer():
    """Verify exception when buffer is shorter than TCP_HEADER_SIZE."""
    short_buf = b"SN\x01\x01\x00"
    with pytest.raises(ValueError, match="too short"):
        TCPHeader.unpack(short_buf)


def test_udp_header_pack_unpack():
    """Verify standard UDPHeader packing and unpacking with chunk metadata."""
    header = UDPHeader(
        msg_type=MsgType.DATA_CHUNK,
        flags=HeaderFlags.LAST_CHUNK | HeaderFlags.KEYFRAME,
        frame_id=105,
        packet_seq_num=1024,
        total_chunks=8,
        chunk_index=7,
        timestamp_ns=2_500_000_000_999_888,
        payload_len=1200,
    )
    packed = header.pack()
    assert len(packed) == UDP_HEADER_SIZE
    assert packed[:2] == MAGIC

    unpacked = UDPHeader.unpack(packed)
    assert unpacked.version == VERSION
    assert unpacked.msg_type == MsgType.DATA_CHUNK
    assert HeaderFlags.LAST_CHUNK in unpacked.flags
    assert HeaderFlags.KEYFRAME in unpacked.flags
    assert unpacked.frame_id == 105
    assert unpacked.packet_seq_num == 1024
    assert unpacked.total_chunks == 8
    assert unpacked.chunk_index == 7
    assert unpacked.timestamp_ns == 2_500_000_000_999_888
    assert unpacked.payload_len == 1200


def test_udp_header_invalid_magic():
    """Verify exception when UDP magic bytes do not match."""
    header = UDPHeader(
        msg_type=MsgType.DATA_FRAME,
        flags=HeaderFlags.NONE,
        frame_id=1,
        packet_seq_num=1,
        total_chunks=1,
        chunk_index=0,
        timestamp_ns=1000,
        payload_len=100,
    )
    packed = bytearray(header.pack())
    packed[1] = ord("Z")  # Corrupt magic byte

    with pytest.raises(ValueError, match="Invalid magic bytes"):
        UDPHeader.unpack(bytes(packed))
