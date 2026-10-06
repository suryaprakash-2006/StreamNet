"""
StreamNet Metrics Collection and Telemetry Engine.

Implements real-time tracking of:
  - Throughput (raw transport bandwidth)
  - Goodput (application-usable data bandwidth)
  - Latency (one-way transit delay using shared monotonic clock)
  - Jitter (RFC 3550 compliant running estimator)
  - Packet Loss Rate and Out-of-Order arrival (UDP sequence tracking)
  - Frame delivery rates and playback stall statistics
"""

from dataclasses import dataclass, field
import threading
import time
from typing import Any, Dict, List, Optional
import numpy as np


@dataclass
class PacketMetric:
    """Individual record for a received packet."""
    seq_num: int
    frame_id: int
    send_time_ns: int
    recv_time_ns: int
    payload_len: int
    total_len: int
    is_out_of_order: bool = False


class MetricsCollector:
    """
    Thread-safe metrics accumulator for StreamNet sessions.
    """

    def __init__(self, protocol: str = "TCP", timing_mode: str = "LOCAL_MONOTONIC_CLOCK"):
        self.protocol = protocol.upper()
        self.timing_mode = timing_mode
        self._lock = threading.Lock()

        # Session timing
        self.start_time_ns: Optional[int] = None
        self.end_time_ns: Optional[int] = None

        # Data volume counters
        self.total_bytes_received: int = 0
        self.total_payload_bytes: int = 0
        self.packets_received: int = 0
        self.frames_completed: int = 0
        self.total_frames_expected: int = 0

        # Sequence and loss tracking (UDP)
        self.min_seq_seen: Optional[int] = None
        self.max_seq_seen: Optional[int] = None
        self.seq_numbers_seen: set[int] = set()
        self.out_of_order_count: int = 0

        # Latency and Jitter tracking
        self.latencies_ms: List[float] = []
        self._last_send_time_ns: Optional[int] = None
        self._last_recv_time_ns: Optional[int] = None
        self.rfc3550_jitter_ms: float = 0.0

        # Playout stall tracking
        self.stall_events: int = 0
        self.total_stall_duration_s: float = 0.0

        # Packet log
        self.packet_log: List[PacketMetric] = []

    def start_session(self) -> None:
        """Mark session initiation timestamp."""
        with self._lock:
            self.start_time_ns = time.perf_counter_ns()

    def record_packet(
        self,
        seq_num: int,
        frame_id: int,
        send_time_ns: int,
        recv_time_ns: int,
        payload_len: int,
        header_len: int,
    ) -> None:
        """
        Record the arrival of a packet/datagram.
        """
        with self._lock:
            if self.start_time_ns is None:
                self.start_time_ns = recv_time_ns

            total_packet_len = header_len + payload_len
            self.total_bytes_received += total_packet_len
            self.total_payload_bytes += payload_len
            self.packets_received += 1

            # Sequence and Out-of-Order tracking
            is_out_of_order = False
            if self.min_seq_seen is None or seq_num < self.min_seq_seen:
                self.min_seq_seen = seq_num
            
            if self.max_seq_seen is not None:
                if seq_num < self.max_seq_seen and seq_num not in self.seq_numbers_seen:
                    is_out_of_order = True
                    self.out_of_order_count += 1
            if self.max_seq_seen is None or seq_num > self.max_seq_seen:
                self.max_seq_seen = seq_num

            self.seq_numbers_seen.add(seq_num)

            # Latency calculation (in milliseconds)
            latency_ms = max(0.0, (recv_time_ns - send_time_ns) / 1_000_000.0)
            self.latencies_ms.append(latency_ms)

            # RFC 3550 Interarrival Jitter Calculation
            # D(i-1, i) = (R_i - R_{i-1}) - (S_i - S_{i-1})
            if self._last_send_time_ns is not None and self._last_recv_time_ns is not None:
                transit_curr_ms = (recv_time_ns - send_time_ns) / 1_000_000.0
                transit_prev_ms = (self._last_recv_time_ns - self._last_send_time_ns) / 1_000_000.0
                d = abs(transit_curr_ms - transit_prev_ms)
                # RFC 3550 smoothing filter: J = J + (|D| - J) / 16
                self.rfc3550_jitter_ms += (d - self.rfc3550_jitter_ms) / 16.0

            self._last_send_time_ns = send_time_ns
            self._last_recv_time_ns = recv_time_ns

            # Append to detailed packet trace
            self.packet_log.append(
                PacketMetric(
                    seq_num=seq_num,
                    frame_id=frame_id,
                    send_time_ns=send_time_ns,
                    recv_time_ns=recv_time_ns,
                    payload_len=payload_len,
                    total_len=total_packet_len,
                    is_out_of_order=is_out_of_order,
                )
            )

    def record_frame_completed(self, frame_id: int) -> None:
        """Increment count of successfully assembled/rendered frames."""
        with self._lock:
            self.frames_completed += 1

    def record_stall(self, duration_s: float) -> None:
        """Record a playout buffer stall/rebuffering event."""
        with self._lock:
            self.stall_events += 1
            self.total_stall_duration_s += duration_s

    def end_session(self) -> None:
        """Mark session completion timestamp."""
        with self._lock:
            self.end_time_ns = time.perf_counter_ns()

    def get_summary(self, total_packets_sent: Optional[int] = None) -> Dict[str, Any]:
        """
        Compute comprehensive performance summary metrics.
        """
        with self._lock:
            start_ns = self.start_time_ns or time.perf_counter_ns()
            end_ns = self.end_time_ns or time.perf_counter_ns()
            duration_s = max((end_ns - start_ns) / 1_000_000_000.0, 1e-6)

            # Throughput & Goodput in Mbps
            throughput_mbps = (self.total_bytes_received * 8.0) / (duration_s * 1_000_000.0)
            goodput_mbps = (self.total_payload_bytes * 8.0) / (duration_s * 1_000_000.0)

            # Packet Loss Calculation
            if total_packets_sent is not None and total_packets_sent > 0:
                expected_packets = total_packets_sent
            elif self.min_seq_seen is not None and self.max_seq_seen is not None:
                expected_packets = max(self.packets_received, (self.max_seq_seen - self.min_seq_seen + 1))
            else:
                expected_packets = self.packets_received

            lost_packets = max(0, expected_packets - self.packets_received)
            loss_rate_percent = (lost_packets / expected_packets * 100.0) if expected_packets > 0 else 0.0

            # Latency Statistics
            if self.latencies_ms:
                latencies_arr = np.array(self.latencies_ms)
                mean_lat = float(np.mean(latencies_arr))
                median_lat = float(np.median(latencies_arr))
                min_lat = float(np.min(latencies_arr))
                max_lat = float(np.max(latencies_arr))
                p95_lat = float(np.percentile(latencies_arr, 95))
                p99_lat = float(np.percentile(latencies_arr, 99))
                std_lat = float(np.std(latencies_arr))
            else:
                mean_lat = median_lat = min_lat = max_lat = p95_lat = p99_lat = std_lat = 0.0

            return {
                "protocol": self.protocol,
                "timing_mode": self.timing_mode,
                "duration_seconds": round(duration_s, 4),
                "total_bytes_received": self.total_bytes_received,
                "total_payload_bytes": self.total_payload_bytes,
                "throughput_mbps": round(throughput_mbps, 4),
                "goodput_mbps": round(goodput_mbps, 4),
                "packets_sent": expected_packets,
                "packets_received": self.packets_received,
                "packets_lost": lost_packets,
                "packet_loss_rate_percent": round(loss_rate_percent, 4),
                "out_of_order_packets": self.out_of_order_count,
                "latency_mean_ms": round(mean_lat, 3),
                "latency_median_ms": round(median_lat, 3),
                "latency_min_ms": round(min_lat, 3),
                "latency_max_ms": round(max_lat, 3),
                "latency_p95_ms": round(p95_lat, 3),
                "latency_p99_ms": round(p99_lat, 3),
                "latency_std_ms": round(std_lat, 3),
                "rfc3550_jitter_ms": round(self.rfc3550_jitter_ms, 3),
                "frames_completed": self.frames_completed,
                "rebuffering_events": self.stall_events,
                "total_stall_seconds": round(self.total_stall_duration_s, 4),
            }
