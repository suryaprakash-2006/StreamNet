"""
StreamNet Protocol Framing and Header Serialization.

Defines binary header formats for TCP stream framing and UDP datagrams.
All integer fields are encoded in big-endian (network byte order).
"""

from dataclasses import dataclass
import enum
import struct
import time
from typing import Optional, Tuple

# Protocol Constants
MAGIC = b"SN"
VERSION = 1

# Header Sizes in Bytes
TCP_HEADER_SIZE = 24
UDP_HEADER_SIZE = 32

# Max safe UDP payload size to stay within standard 1500B Ethernet MTU
MAX_UDP_PAYLOAD_SIZE = 1400


class MsgType(enum.IntEnum):
    """Message type identifiers."""
    DATA_FRAME = 1      # Full frame / payload (standard TCP framing)
    DATA_CHUNK = 2      # Fragmented chunk of a frame (standard UDP datagram)
    STREAM_START = 3    # Handshake / metadata header
    STREAM_END = 4      # End of stream indication
    PING = 5            # Latency / RTT probe
    PONG = 6            # Latency / RTT response


class HeaderFlags(enum.IntFlag):
    """Header flag bitmasks."""
    NONE = 0x00
    KEYFRAME = 0x01
    LAST_CHUNK = 0x02
    EOS = 0x04


@dataclass
class TCPHeader:
    """
    TCP Packet Header (24 Bytes).
    Format: !2sBBB3sIQI
      - magic (2s): b'SN'
      - version (B): 1
      - msg_type (B): MsgType enum value
      - flags (B): HeaderFlags bitmask
      - reserved (3s): b'\\x00\\x00\\x00'
      - frame_id (I): 32-bit unsigned frame identifier
      - timestamp_ns (Q): 64-bit unsigned sender monotonic timestamp in nanoseconds
      - payload_len (I): 32-bit unsigned payload length
    """
    msg_type: MsgType
    flags: HeaderFlags
    frame_id: int
    timestamp_ns: int
    payload_len: int
    version: int = VERSION

    # struct format string
    _STRUCT_FMT = "!2sBBB3sIQI"

    def pack(self) -> bytes:
        return struct.pack(
            self._STRUCT_FMT,
            MAGIC,
            self.version,
            int(self.msg_type),
            int(self.flags),
            b"\x00\x00\x00",
            self.frame_id,
            self.timestamp_ns,
            self.payload_len,
        )

    @classmethod
    def unpack(cls, buffer: bytes) -> "TCPHeader":
        if len(buffer) < TCP_HEADER_SIZE:
            raise ValueError(f"Buffer length {len(buffer)} too short for TCP header (need {TCP_HEADER_SIZE})")

        magic, version, msg_type_val, flags_val, _, frame_id, timestamp_ns, payload_len = struct.unpack(
            cls._STRUCT_FMT, buffer[:TCP_HEADER_SIZE]
        )

        if magic != MAGIC:
            raise ValueError(f"Invalid magic bytes: expected {MAGIC!r}, got {magic!r}")
        if version != VERSION:
            raise ValueError(f"Unsupported protocol version: {version}")

        return cls(
            version=version,
            msg_type=MsgType(msg_type_val),
            flags=HeaderFlags(flags_val),
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            payload_len=payload_len,
        )


@dataclass
class UDPHeader:
    """
    UDP Datagram Header (32 Bytes).
    Format: !2sBBB3sIIHHQI
      - magic (2s): b'SN'
      - version (B): 1
      - msg_type (B): MsgType enum value
      - flags (B): HeaderFlags bitmask
      - reserved (3s): b'\\x00\\x00\\x00'
      - frame_id (I): 32-bit unsigned frame identifier
      - packet_seq_num (I): 32-bit global packet sequence number
      - total_chunks (H): 16-bit total chunks for this frame (1 if unfragmented)
      - chunk_index (H): 16-bit chunk index (0 to total_chunks - 1)
      - timestamp_ns (Q): 64-bit unsigned sender timestamp (nanoseconds)
      - payload_len (I): 32-bit unsigned payload length in this datagram
    """
    msg_type: MsgType
    flags: HeaderFlags
    frame_id: int
    packet_seq_num: int
    total_chunks: int
    chunk_index: int
    timestamp_ns: int
    payload_len: int
    version: int = VERSION

    _STRUCT_FMT = "!2sBBB3sIIHHQI"

    def pack(self) -> bytes:
        return struct.pack(
            self._STRUCT_FMT,
            MAGIC,
            self.version,
            int(self.msg_type),
            int(self.flags),
            b"\x00\x00\x00",
            self.frame_id,
            self.packet_seq_num,
            self.total_chunks,
            self.chunk_index,
            self.timestamp_ns,
            self.payload_len,
        )

    @classmethod
    def unpack(cls, buffer: bytes) -> "UDPHeader":
        if len(buffer) < UDP_HEADER_SIZE:
            raise ValueError(f"Buffer length {len(buffer)} too short for UDP header (need {UDP_HEADER_SIZE})")

        (
            magic,
            version,
            msg_type_val,
            flags_val,
            _,
            frame_id,
            packet_seq_num,
            total_chunks,
            chunk_index,
            timestamp_ns,
            payload_len,
        ) = struct.unpack(cls._STRUCT_FMT, buffer[:UDP_HEADER_SIZE])

        if magic != MAGIC:
            raise ValueError(f"Invalid magic bytes: expected {MAGIC!r}, got {magic!r}")
        if version != VERSION:
            raise ValueError(f"Unsupported protocol version: {version}")

        return cls(
            version=version,
            msg_type=MsgType(msg_type_val),
            flags=HeaderFlags(flags_val),
            frame_id=frame_id,
            packet_seq_num=packet_seq_num,
            total_chunks=total_chunks,
            chunk_index=chunk_index,
            timestamp_ns=timestamp_ns,
            payload_len=payload_len,
        )


def get_current_time_ns() -> int:
    """Return high-resolution monotonic time in nanoseconds."""
    return time.perf_counter_ns()
