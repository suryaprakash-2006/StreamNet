"""
StreamNet Socket-Level Network Impairment Proxy and Channel Emulator.

Provides high-precision simulation of:
  - Propagation delay (latency) using priority release queues with full pipelining
  - Delay variation (jitter) using Gaussian/Uniform noise
  - Packet loss using statistical Bernoulli and burst Markov drop models
  - Bandwidth limitation using Leaky/Token-Bucket bottleneck router queueing with configurable queue capacity and tail-drop
"""

from collections import deque
import heapq
import logging
import random
import socket
import threading
import time
from typing import Deque, List, Optional, Tuple

from network.conditions import NetworkCondition

logger = logging.getLogger("StreamNet.NetworkEmulator")


class UDPEmulatorProxy:
    """
    Socket-level UDP Network Impairment Proxy.
    Intercepts datagrams from sender, applies configured network degradations,
    and forwards to the target receiver.
    """

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 6002,
        target_host: str = "127.0.0.1",
        target_port: int = 5002,
        condition: Optional[NetworkCondition] = None,
        max_queue_delay_s: float = 1.0,  # Max bottleneck router queue buffer before tail-drop
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.condition = condition or NetworkCondition()
        self.max_queue_delay_s = max_queue_delay_s

        self.recv_sock: Optional[socket.socket] = None
        self.send_sock: Optional[socket.socket] = None
        self.is_running = False

        # Priority Queue for scheduled packet release: (release_time_mono, seq_id, data, target_addr)
        self._queue: List[Tuple[float, int, bytes, Tuple[str, int]]] = []
        self._queue_lock = threading.Lock()
        self._queue_cond = threading.Condition(self._queue_lock)
        self._packet_counter = 0

        # Bottleneck router departure scheduler
        self._last_depart_time = 0.0

        # Statistics
        self.packets_forwarded = 0
        self.packets_dropped = 0
        self.bytes_forwarded = 0

        # Threads
        self._recv_thread: Optional[threading.Thread] = None
        self._dispatch_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Initialize sockets and start background proxy threads."""
        self.recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.recv_sock.bind((self.listen_host, self.listen_port))
        self.recv_sock.settimeout(0.5)

        self.send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.is_running = True
        self._last_depart_time = time.perf_counter()

        self._recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, daemon=True)

        self._recv_thread.start()
        self._dispatch_thread.start()
        logger.info(
            "UDP Impairment Proxy active: %s:%d -> %s:%d (Delay: %.1fms, Loss: %.1f%%, BW: %s Mbps)",
            self.listen_host,
            self.listen_port,
            self.target_host,
            self.target_port,
            self.condition.latency_ms,
            self.condition.loss_percent,
            self.condition.bandwidth_mbps if self.condition.bandwidth_mbps else "Unlimited",
        )

    def set_condition(self, condition: NetworkCondition) -> None:
        """Dynamically update network degradation parameters."""
        self.condition = condition
        logger.info("Updated UDP Impairment condition: %s", condition)

    def _receive_loop(self) -> None:
        """Ingest incoming datagrams and schedule degradation."""
        target_addr = (self.target_host, self.target_port)

        while self.is_running:
            try:
                data, src_addr = self.recv_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except (socket.error, OSError):
                break

            # 1. Packet Loss Decision (Statistical Channel Loss)
            if self.condition.loss_percent > 0:
                roll = random.uniform(0.0, 100.0)
                if roll < self.condition.loss_percent:
                    self.packets_dropped += 1
                    logger.debug("Dropped packet of %d bytes (Loss roll: %.2f%% < %.2f%%)",
                                 len(data), roll, self.condition.loss_percent)
                    continue

            # 2. Bandwidth Bottleneck & Transmission Delay Calculation
            now = time.perf_counter()
            packet_len = len(data)

            if self.condition.bandwidth_mbps and self.condition.bandwidth_mbps > 0:
                bytes_per_sec = (self.condition.bandwidth_mbps * 1_000_000.0) / 8.0
                tx_duration = packet_len / bytes_per_sec
                depart_time = max(now, self._last_depart_time) + tx_duration

                # Check bottleneck queue buffer overflow (Tail Drop)
                queueing_delay = depart_time - now
                if queueing_delay > self.max_queue_delay_s:
                    self.packets_dropped += 1
                    logger.debug("Tail-drop: router buffer overflow (delay %.2fs > %.2fs)",
                                 queueing_delay, self.max_queue_delay_s)
                    continue

                self._last_depart_time = depart_time
            else:
                depart_time = now

            # 3. Latency & Jitter Computation
            base_delay_s = self.condition.latency_ms / 1000.0
            jitter_s = 0.0
            if self.condition.jitter_ms > 0:
                jitter_s = random.gauss(0, self.condition.jitter_ms / 1000.0)

            total_delay = max(0.0, base_delay_s + jitter_s)
            target_release_time = depart_time + total_delay

            with self._queue_cond:
                self._packet_counter += 1
                heapq.heappush(
                    self._queue,
                    (target_release_time, self._packet_counter, data, target_addr),
                )
                self._queue_cond.notify()

    def _dispatch_loop(self) -> None:
        """Pop scheduled datagrams from priority queue and dispatch to receiver."""
        while self.is_running:
            with self._queue_cond:
                while self.is_running and not self._queue:
                    self._queue_cond.wait(timeout=0.01)

                if not self.is_running:
                    break

                now = time.perf_counter()
                release_time, p_id, data, target_addr = self._queue[0]

                if now < release_time:
                    wait_s = release_time - now
                    self._queue_cond.wait(timeout=min(wait_s, 0.005))
                    continue

                # Ready to transmit
                heapq.heappop(self._queue)

            try:
                self.send_sock.sendto(data, target_addr)
                self.packets_forwarded += 1
                self.bytes_forwarded += len(data)
            except Exception as err:
                logger.warning("Error forwarding UDP packet: %s", err)

    def stop(self) -> None:
        """Stop proxy threads and release sockets."""
        self.is_running = False
        with self._queue_cond:
            self._queue_cond.notify_all()

        if self.recv_sock:
            try:
                self.recv_sock.close()
            except Exception:
                pass
            self.recv_sock = None

        if self.send_sock:
            try:
                self.send_sock.close()
            except Exception:
                pass
            self.send_sock = None

        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=1.0)
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=1.0)
        logger.info("UDP Impairment Proxy stopped.")


class TCPEmulatorProxy:
    """
    Socket-level TCP Network Impairment Proxy.
    Accepts client connection, bridges to upstream TCP server, and applies
    pipelined traffic shaping (bandwidth throttling, delay/latency injection, and FIFO queue preservation).
    """

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 6001,
        target_host: str = "127.0.0.1",
        target_port: int = 5001,
        condition: Optional[NetworkCondition] = None,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.target_host = target_host
        self.target_port = target_port
        self.condition = condition or NetworkCondition()

        self.server_sock: Optional[socket.socket] = None
        self.is_running = False
        self._main_thread: Optional[threading.Thread] = None

        self.bytes_forwarded = 0

    def start(self) -> None:
        """Start listening for TCP client connection."""
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.server_sock.bind((self.listen_host, self.listen_port))
        self.server_sock.listen(1)
        self.server_sock.settimeout(0.5)

        self.is_running = True
        self._main_thread = threading.Thread(target=self._proxy_loop, daemon=True)
        self._main_thread.start()
        logger.info(
            "TCP Impairment Proxy active: %s:%d -> %s:%d (Delay: %.1fms, BW: %s Mbps)",
            self.listen_host,
            self.listen_port,
            self.target_host,
            self.target_port,
            self.condition.latency_ms,
            self.condition.bandwidth_mbps if self.condition.bandwidth_mbps else "Unlimited",
        )

    def set_condition(self, condition: NetworkCondition) -> None:
        """Dynamically update network degradation parameters."""
        self.condition = condition

    def _proxy_loop(self) -> None:
        """Accept connection and bridge to target server."""
        while self.is_running:
            try:
                client_sock, client_addr = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except socket.timeout:
                continue
            except (socket.error, OSError):
                break

            logger.info("TCP Proxy: client connected from %s", client_addr)

            # Connect upstream to target server
            try:
                upstream_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                upstream_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                upstream_sock.connect((self.target_host, self.target_port))
            except Exception as err:
                logger.error("TCP Proxy failed to connect upstream to %s:%d: %s",
                             self.target_host, self.target_port, err)
                client_sock.close()
                continue

            # Bidirectional forwarding threads
            t1 = threading.Thread(target=self._forward_stream_pipelined, args=(upstream_sock, client_sock), daemon=True)
            t2 = threading.Thread(target=self._forward_stream_direct, args=(client_sock, upstream_sock), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

    def _forward_stream_direct(self, src_sock: socket.socket, dst_sock: socket.socket) -> None:
        """Direct reverse forwarding for control/client messages."""
        try:
            while self.is_running:
                data = src_sock.recv(4096)
                if not data:
                    break
                dst_sock.sendall(data)
        except Exception:
            pass

    def _forward_stream_pipelined(self, src_sock: socket.socket, dst_sock: socket.socket) -> None:
        """
        Forward byte stream with strictly in-order FIFO pipelined scheduling,
        latency injection, and bottleneck transmission delay.
        """
        fifo_queue: Deque[Tuple[float, bytes]] = deque()
        queue_lock = threading.Lock()
        queue_cond = threading.Condition(queue_lock)
        read_active = True

        last_depart_time = 0.0

        def dispatcher():
            nonlocal fifo_queue
            while self.is_running:
                with queue_cond:
                    while self.is_running and not fifo_queue and read_active:
                        queue_cond.wait(timeout=0.01)

                    if not fifo_queue and not read_active:
                        break
                    if not self.is_running:
                        break

                    now = time.perf_counter()
                    rel_time, chunk = fifo_queue[0]
                    if now < rel_time:
                        queue_cond.wait(timeout=min(rel_time - now, 0.005))
                        continue

                    fifo_queue.popleft()

                try:
                    dst_sock.sendall(chunk)
                    self.bytes_forwarded += len(chunk)
                except Exception:
                    break

        dispatch_thread = threading.Thread(target=dispatcher, daemon=True)
        dispatch_thread.start()

        chunk_size = 4096
        try:
            while self.is_running:
                data = src_sock.recv(chunk_size)
                if not data:
                    break

                now = time.perf_counter()
                chunk_len = len(data)

                # Bandwidth transmission departure calculation
                if self.condition.bandwidth_mbps and self.condition.bandwidth_mbps > 0:
                    bytes_per_sec = (self.condition.bandwidth_mbps * 1_000_000.0) / 8.0
                    tx_duration = chunk_len / bytes_per_sec
                    depart_time = max(now, last_depart_time) + tx_duration
                    last_depart_time = depart_time
                else:
                    depart_time = now

                # Base propagation delay
                base_delay_s = self.condition.latency_ms / 1000.0
                scheduled_time = depart_time + base_delay_s

                with queue_cond:
                    fifo_queue.append((scheduled_time, data))
                    queue_cond.notify()

        except Exception:
            pass
        finally:
            read_active = False
            with queue_cond:
                queue_cond.notify_all()
            dispatch_thread.join(timeout=10.0)
            try:
                src_sock.close()
            except Exception:
                pass
            try:
                dst_sock.close()
            except Exception:
                pass

    def stop(self) -> None:
        """Stop TCP proxy."""
        self.is_running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
        logger.info("TCP Impairment Proxy stopped.")
