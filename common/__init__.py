"""StreamNet Common Package."""
from common.protocol import (
    MAGIC,
    VERSION,
    TCP_HEADER_SIZE,
    UDP_HEADER_SIZE,
    MAX_UDP_PAYLOAD_SIZE,
    MsgType,
    HeaderFlags,
    TCPHeader,
    UDPHeader,
    get_current_time_ns,
)

__all__ = [
    "MAGIC",
    "VERSION",
    "TCP_HEADER_SIZE",
    "UDP_HEADER_SIZE",
    "MAX_UDP_PAYLOAD_SIZE",
    "MsgType",
    "HeaderFlags",
    "TCPHeader",
    "UDPHeader",
    "get_current_time_ns",
]
