"""
StreamNet Experiment Runner and Automated Benchmark Orchestrator.

Executes comparative benchmarks across TCP and UDP for configured scenarios,
collects real-time network telemetry, computes statistical summaries,
and persists structured results to JSON and CSV.
"""

import csv
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional

from client.buffer import BufferState, PlayoutBuffer
from client.playback import PlaybackRenderer
from client.tcp_client import TCPClient
from client.udp_client import UDPClient
from experiments.scenarios import (
    ExperimentScenario,
    build_bandwidth_sweep,
    build_baseline_suite,
    build_jitter_sweep,
    build_latency_sweep,
    build_loss_sweep,
    build_profiles_suite,
)
from metrics.collector import MetricsCollector
from network.conditions import NetworkCondition
from network.emulator import TCPEmulatorProxy, UDPEmulatorProxy
from server.tcp_server import TCPServer
from server.udp_server import UDPServer
from server.video_source import VideoSource

logger = logging.getLogger("StreamNet.Runner")

RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


class ExperimentRunner:
    """
    Executes automated comparative experiments over TCP and UDP.
    """

    def __init__(self, results_dir: Optional[Path] = None):
        self.results_dir = results_dir or RESULTS_DIR
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def run_tcp_trial(
        self,
        scenario: ExperimentScenario,
        server_port: int = 5401,
        proxy_port: int = 6401,
    ) -> Dict[str, Any]:
        """Execute a single TCP streaming trial under the scenario's network condition."""
        logger.info("Executing TCP trial for scenario: %s (%s)", scenario.scenario_id, scenario.name)
        condition = scenario.condition

        # 1. Start Server
        server = TCPServer(host="127.0.0.1", port=server_port)
        server.start()

        # 2. Setup Network Impairment (Proxy) if degraded
        proxy = None
        target_client_port = server_port
        if not condition.is_ideal():
            proxy = TCPEmulatorProxy(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                target_host="127.0.0.1",
                target_port=server_port,
                condition=condition,
            )
            proxy.start()
            target_client_port = proxy_port
            time.sleep(0.05)

        # 3. Setup Client, Buffer, Renderer, Metrics
        collector = MetricsCollector(protocol="TCP")
        buffer = PlayoutBuffer(target_fps=scenario.fps, initial_buffer_frames=3, rebuffering_threshold=2)
        renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=True, target_fps=scenario.fps)

        def on_frame(frame_id: int, payload: bytes, header):
            buffer.push_frame(frame_id, payload, header.timestamp_ns)

        client = TCPClient(
            server_host="127.0.0.1",
            server_port=target_client_port,
            metrics_collector=collector,
            on_frame_received=on_frame,
        )

        def client_worker():
            time.sleep(0.05)
            try:
                client.connect()
                client.receive_stream()
            except Exception as e:
                logger.error("TCP Client worker exception: %s", e)

        client_thread = threading.Thread(target=client_worker)
        client_thread.start()
        renderer.start()

        # 4. Stream Video Workload
        source = VideoSource(
            width=scenario.width,
            height=scenario.height,
            fps=scenario.fps,
            jpeg_quality=scenario.jpeg_quality,
        )

        server.stream_payloads(
            source.generate_synthetic_stream(total_frames=scenario.total_frames),
            target_fps=scenario.fps,
        )

        buffer.mark_stream_end()
        client_thread.join(timeout=scenario.total_frames / max(1, scenario.fps) + 10.0)

        # Allow buffer to finish playing
        wait_deadline = time.monotonic() + 3.0
        while buffer.state != BufferState.FINISHED and time.monotonic() < wait_deadline:
            time.sleep(0.02)

        renderer.stop()
        server.stop()
        if proxy:
            proxy.stop()

        # 5. Compile Telemetry
        summary = collector.get_summary(total_packets_sent=server.frames_sent)
        stalls, stall_dur = buffer.get_stall_stats()
        summary["rebuffering_events"] = stalls
        summary["total_stall_seconds"] = round(stall_dur, 4)
        summary["frames_sent"] = server.frames_sent
        summary["frames_delivered"] = renderer.frames_rendered
        summary["frame_delivery_ratio_percent"] = round(
            (renderer.frames_rendered / max(1, server.frames_sent)) * 100.0, 3
        )
        return summary

    def run_udp_trial(
        self,
        scenario: ExperimentScenario,
        client_port: int = 5402,
        proxy_port: int = 6402,
    ) -> Dict[str, Any]:
        """Execute a single UDP streaming trial under the scenario's network condition."""
        logger.info("Executing UDP trial for scenario: %s (%s)", scenario.scenario_id, scenario.name)
        condition = scenario.condition

        # 1. Setup Client, Buffer, Renderer, Metrics
        collector = MetricsCollector(protocol="UDP")
        buffer = PlayoutBuffer(target_fps=scenario.fps, initial_buffer_frames=3, rebuffering_threshold=2)
        renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=True, target_fps=scenario.fps)

        def on_frame(frame_id: int, payload: bytes, header):
            buffer.push_frame(frame_id, payload, header.timestamp_ns)

        client = UDPClient(
            listen_host="127.0.0.1",
            listen_port=client_port,
            metrics_collector=collector,
            on_frame_received=on_frame,
            socket_timeout_s=1.5,
        )
        client.start()

        max_duration = (scenario.total_frames / max(1, scenario.fps)) + 6.0
        client_thread = threading.Thread(target=lambda: client.receive_stream(max_duration_s=max_duration))
        client_thread.start()
        renderer.start()

        # 2. Setup Network Impairment (Proxy)
        proxy = None
        target_server_port = client_port
        if not condition.is_ideal():
            proxy = UDPEmulatorProxy(
                listen_host="127.0.0.1",
                listen_port=proxy_port,
                target_host="127.0.0.1",
                target_port=client_port,
                condition=condition,
            )
            proxy.start()
            target_server_port = proxy_port
            time.sleep(0.05)

        # 3. Stream Video Workload from UDP Server
        server = UDPServer(target_host="127.0.0.1", target_port=target_server_port)
        server.start()

        source = VideoSource(
            width=scenario.width,
            height=scenario.height,
            fps=scenario.fps,
            jpeg_quality=scenario.jpeg_quality,
        )

        server.stream_payloads(
            source.generate_synthetic_stream(total_frames=scenario.total_frames),
            target_fps=scenario.fps,
            max_chunk_size=1400,
        )

        buffer.mark_stream_end()
        client_thread.join(timeout=max_duration + 3.0)

        # Allow buffer to drain
        wait_deadline = time.monotonic() + 2.0
        while buffer.state != BufferState.FINISHED and time.monotonic() < wait_deadline:
            time.sleep(0.02)

        renderer.stop()
        server.stop()
        if proxy:
            proxy.stop()
        client.close()

        # 4. Compile Telemetry
        summary = collector.get_summary(total_packets_sent=server.packets_sent)
        stalls, stall_dur = buffer.get_stall_stats()
        summary["rebuffering_events"] = stalls
        summary["total_stall_seconds"] = round(stall_dur, 4)
        summary["frames_sent"] = server.frames_sent
        summary["frames_delivered"] = renderer.frames_rendered
        summary["frame_delivery_ratio_percent"] = round(
            (renderer.frames_rendered / max(1, server.frames_sent)) * 100.0, 3
        )
        return summary

    def run_scenario(self, scenario: ExperimentScenario) -> Dict[str, Any]:
        """
        Run paired TCP and UDP benchmark for a scenario and persist records.
        """
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        record_id = f"{scenario.scenario_id}_{timestamp_str}"

        # Execute TCP Trial
        tcp_metrics = self.run_tcp_trial(scenario)
        time.sleep(0.2)

        # Execute UDP Trial
        udp_metrics = self.run_udp_trial(scenario)
        time.sleep(0.2)

        result_record = {
            "record_id": record_id,
            "scenario_id": scenario.scenario_id,
            "scenario_name": scenario.name,
            "category": scenario.category,
            "timestamp": datetime.now().isoformat(),
            "condition": {
                "name": scenario.condition.name,
                "latency_ms": scenario.condition.latency_ms,
                "jitter_ms": scenario.condition.jitter_ms,
                "loss_percent": scenario.condition.loss_percent,
                "bandwidth_mbps": scenario.condition.bandwidth_mbps,
            },
            "workload": {
                "total_frames": scenario.total_frames,
                "fps": scenario.fps,
                "resolution": f"{scenario.width}x{scenario.height}",
                "jpeg_quality": scenario.jpeg_quality,
            },
            "tcp_results": tcp_metrics,
            "udp_results": udp_metrics,
        }

        # Save single JSON
        json_file = self.results_dir / f"{record_id}.json"
        with open(json_file, "w") as f:
            json.dump(result_record, f, indent=2)

        # Append to Master CSV
        self._append_to_master_csv(result_record)

        logger.info("Finished scenario %s. Results saved to %s", scenario.scenario_id, json_file)
        return result_record

    def _append_to_master_csv(self, record: Dict[str, Any]) -> None:
        """Append benchmark record to master results CSV."""
        csv_file = self.results_dir / "master_experiment_results.csv"
        file_exists = csv_file.exists()

        rows = []
        for proto in ["TCP", "UDP"]:
            res = record["tcp_results"] if proto == "TCP" else record["udp_results"]
            row = {
                "record_id": record["record_id"],
                "scenario_id": record["scenario_id"],
                "scenario_name": record["scenario_name"],
                "category": record["category"],
                "timestamp": record["timestamp"],
                "protocol": proto,
                "condition_name": record["condition"]["name"],
                "latency_ms": record["condition"]["latency_ms"],
                "jitter_ms": record["condition"]["jitter_ms"],
                "loss_percent": record["condition"]["loss_percent"],
                "bandwidth_mbps": record["condition"]["bandwidth_mbps"] or -1,
                "duration_s": res["duration_seconds"],
                "throughput_mbps": res["throughput_mbps"],
                "goodput_mbps": res["goodput_mbps"],
                "packets_sent": res["packets_sent"],
                "packets_received": res["packets_received"],
                "packets_lost": res["packets_lost"],
                "packet_loss_rate_percent": res["packet_loss_rate_percent"],
                "latency_mean_ms": res["latency_mean_ms"],
                "latency_p95_ms": res["latency_p95_ms"],
                "latency_p99_ms": res["latency_p99_ms"],
                "rfc3550_jitter_ms": res["rfc3550_jitter_ms"],
                "frames_sent": res["frames_sent"],
                "frames_delivered": res["frames_delivered"],
                "frame_delivery_ratio_percent": res["frame_delivery_ratio_percent"],
                "rebuffering_events": res["rebuffering_events"],
                "total_stall_seconds": res["total_stall_seconds"],
            }
            rows.append(row)

        fieldnames = list(rows[0].keys())
        with open(csv_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(rows)

    def run_suite(self, scenarios: List[ExperimentScenario]) -> List[Dict[str, Any]]:
        """Run an entire suite of benchmark scenarios sequentially."""
        results = []
        for idx, scenario in enumerate(scenarios, 1):
            logger.info("=== Starting Benchmark %d / %d: %s ===", idx, len(scenarios), scenario.name)
            res = self.run_scenario(scenario)
            results.append(res)
        return results


def run_all_benchmarks(total_frames: int = 90, fps: int = 30) -> None:
    """Convenience entrypoint to execute all experiment sweeps."""
    runner = ExperimentRunner()

    logger.info("Initializing full StreamNet Benchmark Matrix...")
    suites = [
        ("Baseline", build_baseline_suite(total_frames=total_frames, fps=fps)),
        ("Latency Sweep", build_latency_sweep(total_frames=total_frames, fps=fps)),
        ("Loss Sweep", build_loss_sweep(total_frames=total_frames, fps=fps)),
        ("Bandwidth Sweep", build_bandwidth_sweep(total_frames=total_frames, fps=fps)),
        ("Jitter Sweep", build_jitter_sweep(total_frames=total_frames, fps=fps)),
        ("Profiles Suite", build_profiles_suite(total_frames=total_frames, fps=fps)),
    ]

    for name, suite in suites:
        print(f"\n=======================================================")
        print(f"  EXECUTING BENCHMARK SUITE: {name.upper()}")
        print(f"=======================================================")
        runner.run_suite(suite)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_all_benchmarks(total_frames=60, fps=30)
