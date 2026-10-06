"""
StreamNet UDP Streaming Client.

Listens for UDP datagrams, extracts application-level sequence numbers and timestamps,
performs chunk reassembly, detects loss and out-of-order arrivals, and records telemetry.
"""

from collections import defaultdict
import logging
import socket
import time
from typing import Callable, Dict, Optional

from common.protocol import (
    MAX_UDP_PAYLOAD_SIZE,
    UDP_HEADER_SIZE,
    HeaderFlags,
    MsgType,
    UDPHeader,
    get_current_time_ns,
)
from metrics.collector import MetricsCollector

logger = logging.getLogger("StreamNet.UDPClient")


class UDPClient:
    """
    UDP Client that receives datagram streams, reconstructs frames, and tracks network performance.
    """

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 5002,
        metrics_collector: Optional[MetricsCollector] = None,
        on_frame_received: Optional[Callable[[int, bytes, UDPHeader], None]] = None,
        socket_timeout_s: float = 2.0,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.metrics = metrics_collector or MetricsCollector(protocol="UDP")
        self.on_frame_received = on_frame_received
        self.socket_timeout_s = socket_timeout_s

        self.sock: Optional[socket.socket] = None
        self.is_running = False

        # In-flight frame reassembly buffer: frame_id -> {chunk_index: chunk_bytes}
        self.reassembly_buffer: Dict[int, Dict[int, bytes]] = defaultdict(dict)
        self.frame_total_chunks: Dict[int, int] = {}

    def start(self) -> None:
        """Bind UDP socket to listening address."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.listen_host, self.listen_port))
        self.sock.settimeout(self.socket_timeout_s)
        self.is_running = True
        logger.info("UDP Client listening on %s:%d", self.listen_host, self.listen_port)

    def receive_stream(self, max_duration_s: Optional[float] = None) -> None:
        """
        Receive datagrams until STREAM_END is received, socket times out, or max_duration_s expires.
        """
        if not self.sock:
            raise RuntimeError("UDP Client not started. Call start() first.")

        self.metrics.start_session()
        start_mono = time.monotonic()
        consecutive_timeouts = 0
        max_timeouts = 3

        try:
            while self.is_running:
                if max_duration_s and (time.monotonic() - start_mono) > max_duration_s:
                    logger.info("Max duration reached (%s seconds).", max_duration_s)
                    break

                try:
                    data, addr = self.sock.recvfrom(UDP_HEADER_SIZE + MAX_UDP_PAYLOAD_SIZE + 256)
                    recv_time_ns = get_current_time_ns()
                    consecutive_timeouts = 0
                except socket.timeout:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= max_timeouts:
                        logger.info("Socket timeout limit reached. Ending stream reception.")
                        break
                    continue

                if len(data) < UDP_HEADER_SIZE:
                    logger.warning("Received runt UDP packet (< %d bytes)", UDP_HEADER_SIZE)
                    continue

                try:
                    header = UDPHeader.unpack(data[:UDP_HEADER_SIZE])
                except ValueError as err:
                    logger.warning("Failed to parse UDP header: %s", err)
                    continue

                # Handle End of Stream
                if header.msg_type == MsgType.STREAM_END or (header.flags & HeaderFlags.EOS):
                    logger.info("Received End-of-Stream signal from UDP server.")
                    break

                payload = data[UDP_HEADER_SIZE : UDP_HEADER_SIZE + header.payload_len]

                # Record datagram metrics
                self.metrics.record_packet(
                    seq_num=header.packet_seq_num,
                    frame_id=header.frame_id,
                    send_time_ns=header.timestamp_ns,
                    recv_time_ns=recv_time_ns,
                    payload_len=header.payload_len,
                    header_len=UDP_HEADER_SIZE,
                )

                # Frame reassembly
                frame_id = header.frame_id
                self.frame_total_chunks[frame_id] = header.total_chunks
                self.reassembly_buffer[frame_id][header.chunk_index] = payload

                # Check if all chunks for this frame have arrived
                if len(self.reassembly_buffer[frame_id]) == header.total_chunks:
                    # Assemble full frame payload in chunk index order
                    assembled_chunks = [
                        self.reassembly_buffer[frame_id][i]
                        for i in range(header.total_chunks)
                    ]
                    full_frame_payload = b"".join(assembled_chunks)

                    self.metrics.record_frame_completed(frame_id)

                    if self.on_frame_received:
                        self.on_frame_received(frame_id, full_frame_payload, header)

                    # Clean up reassembly storage
                    del self.reassembly_buffer[frame_id]
                    del self.frame_total_chunks[frame_id]

        except Exception as err:
            logger.error("Error during UDP stream reception: %s", err)
        finally:
            self.metrics.end_session()
            self.close()

    def close(self) -> None:
        """Close UDP socket."""
        self.is_running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        logger.info("UDP Client stopped.")
