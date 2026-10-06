"""
StreamNet TCP Streaming Server.

Implements TCP streaming using binary application-layer framing (TCPHeader).
"""

import logging
import socket
import threading
import time
from typing import Callable, Generator, Optional, Tuple

from common.protocol import (
    TCP_HEADER_SIZE,
    HeaderFlags,
    MsgType,
    TCPHeader,
    get_current_time_ns,
)

logger = logging.getLogger("StreamNet.TCPServer")


class TCPServer:
    """
    TCP Server that listens for client connections and streams framed payloads.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5001):
        self.host = host
        self.port = port
        self.server_sock: Optional[socket.socket] = None
        self.is_running = False
        self._server_thread: Optional[threading.Thread] = None

        # Statistics
        self.frames_sent = 0
        self.bytes_sent = 0

    def start(self) -> None:
        """Start listening on the configured host and port."""
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Disable Nagle's algorithm for low-latency streaming transmission
        self.server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(1.0)
        self.is_running = True
        logger.info("TCP Server listening on %s:%d", self.host, self.port)

    def stream_payloads(
        self,
        payload_generator: Generator[Tuple[int, bytes, HeaderFlags], None, None],
        target_fps: float = 30.0,
    ) -> None:
        """
        Accept a single client connection and stream payloads from the generator.
        Runs synchronously until the generator finishes or client disconnects.
        """
        if not self.server_sock:
            raise RuntimeError("Server not started. Call start() first.")

        logger.info("Waiting for TCP client connection...")
        client_sock = None
        while self.is_running and client_sock is None:
            try:
                client_sock, client_addr = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                logger.info("TCP Client connected from %s", client_addr)
            except socket.timeout:
                continue

        if not client_sock:
            return

        frame_interval_s = 1.0 / target_fps if target_fps > 0 else 0.0

        try:
            for frame_id, payload, flags in payload_generator:
                if not self.is_running:
                    break

                send_time_ns = get_current_time_ns()
                header = TCPHeader(
                    msg_type=MsgType.DATA_FRAME,
                    flags=flags,
                    frame_id=frame_id,
                    timestamp_ns=send_time_ns,
                    payload_len=len(payload),
                )
                packet_bytes = header.pack() + payload

                # Send framed packet
                client_sock.sendall(packet_bytes)
                self.frames_sent += 1
                self.bytes_sent += len(packet_bytes)

                # Frame rate pacing
                if frame_interval_s > 0:
                    elapsed_s = (get_current_time_ns() - send_time_ns) / 1e9
                    sleep_time = frame_interval_s - elapsed_s
                    if sleep_time > 0:
                        time.sleep(sleep_time)

            # Send End of Stream marker
            eos_header = TCPHeader(
                msg_type=MsgType.STREAM_END,
                flags=HeaderFlags.EOS,
                frame_id=self.frames_sent,
                timestamp_ns=get_current_time_ns(),
                payload_len=0,
            )
            client_sock.sendall(eos_header.pack())

        except (socket.error, ConnectionResetError, BrokenPipeError) as err:
            logger.warning("TCP Client connection error: %s", err)
        finally:
            if client_sock:
                try:
                    client_sock.close()
                except Exception:
                    pass
            logger.info("TCP Streaming finished. Total frames: %d, Total bytes: %d", self.frames_sent, self.bytes_sent)

    def stop(self) -> None:
        """Stop server and release socket."""
        self.is_running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
        logger.info("TCP Server stopped.")
