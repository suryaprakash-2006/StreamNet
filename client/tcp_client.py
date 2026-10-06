"""
StreamNet TCP Streaming Client.

Connects to TCP streaming server, extracts framed packets using exact-byte stream parsing,
and records telemetry into MetricsCollector.
"""

import logging
import socket
import time
from typing import Callable, Optional

from common.protocol import (
    TCP_HEADER_SIZE,
    HeaderFlags,
    MsgType,
    TCPHeader,
    get_current_time_ns,
)
from metrics.collector import MetricsCollector

logger = logging.getLogger("StreamNet.TCPClient")


def recv_exact(sock: socket.socket, num_bytes: int) -> Optional[bytes]:
    """
    Read exactly num_bytes from a TCP socket.
    Returns None if EOF / connection is closed before all bytes are read.
    """
    buf = bytearray()
    while len(buf) < num_bytes:
        try:
            chunk = sock.recv(num_bytes - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        except socket.timeout:
            continue
        except (socket.error, ConnectionResetError):
            return None
    return bytes(buf)


class TCPClient:
    """
    TCP Client that connects to a StreamNet TCP server and receives framed media.
    """

    def __init__(
        self,
        server_host: str = "127.0.0.1",
        server_port: int = 5001,
        metrics_collector: Optional[MetricsCollector] = None,
        on_frame_received: Optional[Callable[[int, bytes, TCPHeader], None]] = None,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.metrics = metrics_collector or MetricsCollector(protocol="TCP")
        self.on_frame_received = on_frame_received

        self.sock: Optional[socket.socket] = None
        self.is_running = False

    def connect(self, timeout: float = 5.0) -> None:
        """Establish connection to TCP server."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.connect((self.server_host, self.server_port))
        self.sock.settimeout(2.0)  # Timeout for frame reads
        self.is_running = True
        logger.info("Connected to TCP server at %s:%d", self.server_host, self.server_port)

    def receive_stream(self) -> None:
        """
        Continuous stream receiving loop.
        Parses 24-byte TCPHeader, then reads exactly payload_len bytes.
        """
        if not self.sock:
            raise RuntimeError("Client not connected. Call connect() first.")

        self.metrics.start_session()

        try:
            while self.is_running:
                header_bytes = recv_exact(self.sock, TCP_HEADER_SIZE)
                if not header_bytes:
                    logger.info("TCP Server closed connection (EOF).")
                    break

                recv_time_ns = get_current_time_ns()
                header = TCPHeader.unpack(header_bytes)

                if header.msg_type == MsgType.STREAM_END or (header.flags & HeaderFlags.EOS):
                    logger.info("Received End-of-Stream signal from TCP server.")
                    break

                # Read payload
                payload = b""
                if header.payload_len > 0:
                    payload_bytes = recv_exact(self.sock, header.payload_len)
                    if payload_bytes is None:
                        logger.warning("Connection closed while reading TCP payload.")
                        break
                    payload = payload_bytes

                # Record metrics
                self.metrics.record_packet(
                    seq_num=header.frame_id,
                    frame_id=header.frame_id,
                    send_time_ns=header.timestamp_ns,
                    recv_time_ns=recv_time_ns,
                    payload_len=header.payload_len,
                    header_len=TCP_HEADER_SIZE,
                )
                self.metrics.record_frame_completed(header.frame_id)

                if self.on_frame_received:
                    self.on_frame_received(header.frame_id, payload, header)

        except Exception as err:
            logger.error("Error during TCP stream reception: %s", err)
        finally:
            self.metrics.end_session()
            self.close()

    def close(self) -> None:
        """Close socket connection."""
        self.is_running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        logger.info("TCP Client disconnected.")
