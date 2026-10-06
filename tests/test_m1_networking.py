"""
End-to-End Loopback Integration Tests for Milestone 1 Networking.

Verifies:
  1. TCP Client/Server communication, application framing, and metric collection.
  2. UDP Client/Server communication, sequence numbering, chunk reassembly, and metric collection.
"""

import threading
import time
from typing import Generator, List, Tuple
import pytest

from client.tcp_client import TCPClient
from client.udp_client import UDPClient
from common.protocol import HeaderFlags
from metrics.collector import MetricsCollector
from server.tcp_server import TCPServer
from server.udp_server import UDPServer


def sample_payload_generator(num_frames: int = 20, frame_size: int = 2048) -> Generator[Tuple[int, bytes, HeaderFlags], None, None]:
    """Generate deterministic test payloads."""
    for i in range(num_frames):
        # Create patterned payload
        pattern = bytes([i % 256]) * frame_size
        flags = HeaderFlags.KEYFRAME if i % 10 == 0 else HeaderFlags.NONE
        yield (i, pattern, flags)


def test_tcp_streaming_loopback():
    """Verify full TCP streaming session, frame integrity, and metrics."""
    port = 5101
    num_frames = 25
    frame_size = 4096

    server = TCPServer(host="127.0.0.1", port=port)
    server.start()

    received_frames: List[Tuple[int, bytes]] = []
    collector = MetricsCollector(protocol="TCP")

    def on_frame(frame_id: int, payload: bytes, header):
        received_frames.append((frame_id, payload))

    client = TCPClient(
        server_host="127.0.0.1",
        server_port=port,
        metrics_collector=collector,
        on_frame_received=on_frame,
    )

    # Run client reception in thread
    def run_client():
        time.sleep(0.05)  # Allow server socket to be active
        client.connect()
        client.receive_stream()

    client_thread = threading.Thread(target=run_client)
    client_thread.start()

    # Stream payloads from server
    server.stream_payloads(
        sample_payload_generator(num_frames=num_frames, frame_size=frame_size),
        target_fps=100.0,
    )

    client_thread.join(timeout=3.0)
    server.stop()

    assert len(received_frames) == num_frames
    for idx, (f_id, payload) in enumerate(received_frames):
        assert f_id == idx
        assert len(payload) == frame_size
        assert payload == bytes([idx % 256]) * frame_size

    summary = collector.get_summary()
    assert summary["protocol"] == "TCP"
    assert summary["packets_received"] == num_frames
    assert summary["packets_lost"] == 0
    assert summary["total_payload_bytes"] == num_frames * frame_size
    assert summary["throughput_mbps"] > 0


def test_udp_streaming_loopback():
    """Verify full UDP streaming session, fragmentation reassembly, and metrics."""
    port = 5102
    num_frames = 20
    frame_size = 3500  # Will be fragmented into 3 chunks (1400 + 1400 + 700)

    client_collector = MetricsCollector(protocol="UDP")
    received_frames: List[Tuple[int, bytes]] = []

    def on_frame(frame_id: int, payload: bytes, header):
        received_frames.append((frame_id, payload))

    client = UDPClient(
        listen_host="127.0.0.1",
        listen_port=port,
        metrics_collector=client_collector,
        on_frame_received=on_frame,
        socket_timeout_s=1.0,
    )
    client.start()

    client_thread = threading.Thread(target=lambda: client.receive_stream(max_duration_s=2.0))
    client_thread.start()

    time.sleep(0.05)

    server = UDPServer(target_host="127.0.0.1", target_port=port)
    server.start()
    server.stream_payloads(
        sample_payload_generator(num_frames=num_frames, frame_size=frame_size),
        target_fps=100.0,
        max_chunk_size=1400,
    )

    client_thread.join(timeout=3.0)
    server.stop()
    client.close()

    assert len(received_frames) == num_frames
    for idx, (f_id, payload) in enumerate(received_frames):
        assert f_id == idx
        assert len(payload) == frame_size
        assert payload == bytes([idx % 256]) * frame_size

    summary = client_collector.get_summary(total_packets_sent=server.packets_sent)
    assert summary["protocol"] == "UDP"
    assert summary["frames_completed"] == num_frames
    assert summary["packets_received"] == server.packets_sent
    assert summary["packets_lost"] == 0
    assert summary["packet_loss_rate_percent"] == 0.0
    assert summary["throughput_mbps"] > 0
