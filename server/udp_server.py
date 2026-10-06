"""
StreamNet UDP Streaming Server.

Implements UDP datagram streaming with application-level sequence numbers,
timestamps, and MTU chunking without artificial transport-layer ACKs.
"""

import logging
import socket
import time
from typing import Generator, Optional, Tuple

from common.protocol import (
    MAX_UDP_PAYLOAD_SIZE,
    UDP_HEADER_SIZE,
    HeaderFlags,
    MsgType,
    UDPHeader,
    get_current_time_ns,
)

logger = logging.getLogger("StreamNet.UDPServer")


class UDPServer:
    """
    UDP Server that sends datagram streams with sequence metadata to a client endpoint.
    """

    def __init__(self, target_host: str = "127.0.0.1", target_port: int = 5002):
        self.target_host = target_host
        self.target_port = target_port
        self.sock: Optional[socket.socket] = None
        self.is_running = False

        # Monotonic sequence counter across the entire stream session
        self.packet_seq_num = 0
        self.frames_sent = 0
        self.packets_sent = 0
        self.bytes_sent = 0

    def start(self) -> None:
        """Initialize UDP socket."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.is_running = True
        logger.info("UDP Server ready. Target: %s:%d", self.target_host, self.target_port)

    def stream_payloads(
        self,
        payload_generator: Generator[Tuple[int, bytes, HeaderFlags], None, None],
        target_fps: float = 30.0,
        max_chunk_size: int = MAX_UDP_PAYLOAD_SIZE,
    ) -> None:
        """
        Stream payloads to the target UDP endpoint.
        If a payload exceeds max_chunk_size, it is fragmented into numbered chunks.
        """
        if not self.sock:
            raise RuntimeError("UDP Server not started. Call start() first.")

        frame_interval_s = 1.0 / target_fps if target_fps > 0 else 0.0
        target_addr = (self.target_host, self.target_port)

        try:
            for frame_id, payload, flags in payload_generator:
                if not self.is_running:
                    break

                send_time_ns = get_current_time_ns()
                payload_len = len(payload)

                # Compute chunk count
                if payload_len == 0:
                    chunks = [b""]
                else:
                    chunks = [
                        payload[i : i + max_chunk_size]
                        for i in range(0, payload_len, max_chunk_size)
                    ]
                total_chunks = len(chunks)

                for chunk_idx, chunk_data in enumerate(chunks):
                    chunk_flags = flags
                    if chunk_idx == total_chunks - 1:
                        chunk_flags |= HeaderFlags.LAST_CHUNK

                    header = UDPHeader(
                        msg_type=MsgType.DATA_CHUNK if total_chunks > 1 else MsgType.DATA_FRAME,
                        flags=chunk_flags,
                        frame_id=frame_id,
                        packet_seq_num=self.packet_seq_num,
                        total_chunks=total_chunks,
                        chunk_index=chunk_idx,
                        timestamp_ns=send_time_ns,
                        payload_len=len(chunk_data),
                    )

                    datagram = header.pack() + chunk_data
                    self.sock.sendto(datagram, target_addr)

                    self.packet_seq_num += 1
                    self.packets_sent += 1
                    self.bytes_sent += len(datagram)

                self.frames_sent += 1

                # Frame rate pacing
                if frame_interval_s > 0:
                    elapsed_s = (get_current_time_ns() - send_time_ns) / 1e9
                    sleep_time = frame_interval_s - elapsed_s
                    if sleep_time > 0:
                        time.sleep(sleep_time)

            # Send End of Stream datagram (sent 3 times for robustness against loss)
            for _ in range(3):
                eos_header = UDPHeader(
                    msg_type=MsgType.STREAM_END,
                    flags=HeaderFlags.EOS,
                    frame_id=self.frames_sent,
                    packet_seq_num=self.packet_seq_num,
                    total_chunks=1,
                    chunk_index=0,
                    timestamp_ns=get_current_time_ns(),
                    payload_len=0,
                )
                self.sock.sendto(eos_header.pack(), target_addr)
                self.packet_seq_num += 1
                time.sleep(0.005)

        except (socket.error, ConnectionResetError) as err:
            logger.warning("UDP Server socket error: %s", err)
        finally:
            logger.info("UDP Streaming finished. Frames: %d, Packets: %d, Bytes: %d",
                        self.frames_sent, self.packets_sent, self.bytes_sent)

    def stop(self) -> None:
        """Stop server and release socket."""
        self.is_running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        logger.info("UDP Server stopped.")
