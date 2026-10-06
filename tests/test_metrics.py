"""
Unit tests for MetricsCollector and RFC 3550 Jitter calculations.
"""

import pytest
from metrics.collector import MetricsCollector


def test_metrics_collector_basic():
    """Verify basic throughput, goodput, and latency aggregation."""
    collector = MetricsCollector(protocol="TCP")
    collector.start_session()

    # Simulate 10 packets received over 100ms
    base_time_ns = 1_000_000_000
    for i in range(10):
        send_t = base_time_ns + (i * 10_000_000)  # Every 10ms
        recv_t = send_t + 2_000_000  # 2ms latency
        collector.record_packet(
            seq_num=i,
            frame_id=i,
            send_time_ns=send_t,
            recv_time_ns=recv_t,
            payload_len=1000,
            header_len=24,
        )
        collector.record_frame_completed(i)

    collector.end_session()
    summary = collector.get_summary()

    assert summary["protocol"] == "TCP"
    assert summary["packets_received"] == 10
    assert summary["packets_lost"] == 0
    assert summary["packet_loss_rate_percent"] == 0.0
    assert summary["frames_completed"] == 10
    assert summary["total_payload_bytes"] == 10000
    assert summary["total_bytes_received"] == 10240
    assert pytest.approx(summary["latency_mean_ms"], rel=1e-2) == 2.0


def test_udp_packet_loss_and_out_of_order():
    """Verify detection of sequence gaps (packet loss) and out-of-order packets."""
    collector = MetricsCollector(protocol="UDP")
    collector.start_session()

    base_time_ns = 1_000_000_000
    # Send sequence: 0, 1, 2, 4, 3, 6 (5 is lost, 3 is out of order)
    sequence = [0, 1, 2, 4, 3, 6]
    for idx, seq in enumerate(sequence):
        send_t = base_time_ns + (idx * 10_000_000)
        recv_t = send_t + 5_000_000
        collector.record_packet(
            seq_num=seq,
            frame_id=seq,
            send_time_ns=send_t,
            recv_time_ns=recv_t,
            payload_len=1400,
            header_len=32,
        )

    collector.end_session()
    summary = collector.get_summary()

    # Sequence range: 0 to 6 -> 7 expected packets. Received 6. Lost 1 (seq 5).
    assert summary["packets_sent"] == 7
    assert summary["packets_received"] == 6
    assert summary["packets_lost"] == 1
    assert pytest.approx(summary["packet_loss_rate_percent"], abs=0.1) == (1 / 7 * 100.0)
    assert summary["out_of_order_packets"] == 1  # seq 3 arrived after seq 4


def test_rfc3550_jitter_calculation():
    """Verify RFC 3550 running jitter calculation with varying delays."""
    collector = MetricsCollector(protocol="UDP")
    collector.start_session()

    # Packet 1: send 0, recv 10 (delay 10ms)
    collector.record_packet(0, 0, send_time_ns=0, recv_time_ns=10_000_000, payload_len=100, header_len=32)
    # Packet 2: send 20, recv 40 (delay 20ms) -> D = |20 - 10| = 10ms -> J = 0 + (10 - 0)/16 = 0.625ms
    collector.record_packet(1, 1, send_time_ns=20_000_000, recv_time_ns=40_000_000, payload_len=100, header_len=32)

    assert pytest.approx(collector.rfc3550_jitter_ms, rel=1e-3) == 0.625
