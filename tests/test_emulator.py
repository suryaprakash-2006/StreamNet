"""
Unit and integration tests for Network Degradation Proxy (UDPEmulatorProxy & TCPEmulatorProxy).
"""

import threading
import time
import pytest

from client.tcp_client import TCPClient
from client.udp_client import UDPClient
from common.protocol import HeaderFlags
from metrics.collector import MetricsCollector
from network.conditions import NetworkCondition
from network.emulator import TCPEmulatorProxy, UDPEmulatorProxy
from server.tcp_server import TCPServer
from server.udp_server import UDPServer


def sample_generator(count: int = 20, size: int = 1000):
    for i in range(count):
        yield (i, b"X" * size, HeaderFlags.NONE)


def test_udp_emulator_latency_injection():
    """Verify that UDP proxy delays packet arrivals according to configured latency."""
    proxy_port = 6102
    target_port = 5302
    target_latency_ms = 40.0

    condition = NetworkCondition(latency_ms=target_latency_ms, loss_percent=0.0)
    proxy = UDPEmulatorProxy(
        listen_host="127.0.0.1",
        listen_port=proxy_port,
        target_host="127.0.0.1",
        target_port=target_port,
        condition=condition,
    )
    proxy.start()

    collector = MetricsCollector(protocol="UDP")
    client = UDPClient(listen_host="127.0.0.1", listen_port=target_port, metrics_collector=collector)
    client.start()

    client_thread = threading.Thread(target=lambda: client.receive_stream(max_duration_s=2.0))
    client_thread.start()

    server = UDPServer(target_host="127.0.0.1", target_port=proxy_port)
    server.start()
    server.stream_payloads(sample_generator(count=10, size=500), target_fps=50.0)

    client_thread.join(timeout=3.0)
    server.stop()
    proxy.stop()
    client.close()

    summary = collector.get_summary()
    assert summary["packets_received"] > 0
    # Average latency should be close to 40ms (+- 15ms allowance for scheduling precision)
    assert summary["latency_mean_ms"] >= 30.0


def test_udp_emulator_packet_loss():
    """Verify statistical packet dropping through the UDP proxy."""
    proxy_port = 6103
    target_port = 5303
    loss_rate = 25.0

    condition = NetworkCondition(latency_ms=0.0, loss_percent=loss_rate)
    proxy = UDPEmulatorProxy(
        listen_host="127.0.0.1",
        listen_port=proxy_port,
        target_host="127.0.0.1",
        target_port=target_port,
        condition=condition,
    )
    proxy.start()

    collector = MetricsCollector(protocol="UDP")
    client = UDPClient(listen_host="127.0.0.1", listen_port=target_port, metrics_collector=collector)
    client.start()

    client_thread = threading.Thread(target=lambda: client.receive_stream(max_duration_s=2.0))
    client_thread.start()

    server = UDPServer(target_host="127.0.0.1", target_port=proxy_port)
    server.start()
    server.stream_payloads(sample_generator(count=40, size=500), target_fps=100.0)

    client_thread.join(timeout=3.0)
    server.stop()
    proxy.stop()
    client.close()

    summary = collector.get_summary(total_packets_sent=server.packets_sent)
    assert proxy.packets_dropped > 0
    assert summary["packets_lost"] > 0
    # Loss percent should reflect approximately 25% (between 10% and 45% for small 40 packet sample)
    assert summary["packet_loss_rate_percent"] >= 10.0


def test_tcp_emulator_bandwidth_throttling():
    """Verify that TCP proxy throttles throughput when bandwidth limitation is applied."""
    server_port = 5304
    proxy_port = 6104
    bw_cap_mbps = 2.0

    condition = NetworkCondition(latency_ms=5.0, bandwidth_mbps=bw_cap_mbps)
    proxy = TCPEmulatorProxy(
        listen_host="127.0.0.1",
        listen_port=proxy_port,
        target_host="127.0.0.1",
        target_port=server_port,
        condition=condition,
    )
    proxy.start()

    server = TCPServer(host="127.0.0.1", port=server_port)
    server.start()

    collector = MetricsCollector(protocol="TCP")
    client = TCPClient(server_host="127.0.0.1", server_port=proxy_port, metrics_collector=collector)

    client_thread = threading.Thread(target=lambda: (time.sleep(0.05), client.connect(), client.receive_stream()))
    client_thread.start()

    # Stream 10 frames of 40KB each (400 KB total)
    server.stream_payloads(sample_generator(count=10, size=40_000), target_fps=100.0)

    client_thread.join(timeout=10.0)
    server.stop()
    proxy.stop()

    summary = collector.get_summary()
    assert summary["packets_received"] == 10
    # Throughput should be throttled around or below ~2.5 Mbps
    assert summary["throughput_mbps"] <= 3.5
