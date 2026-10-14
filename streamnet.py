"""
StreamNet Unified Command-Line Interface (CLI).

Provides unified commands to:
  - Run streaming server (TCP or UDP)
  - Run streaming client with live HUD video playback or headless telemetry
  - Run standalone network impairment proxy
  - Execute automated benchmark sweeps
  - Render publication-grade comparison plots
  - Launch web dashboard
"""

import argparse
import logging
import sys
import time

from client.buffer import BufferState, PlayoutBuffer
from client.playback import PlaybackRenderer
from client.tcp_client import TCPClient
from client.udp_client import UDPClient
from experiments.runner import ExperimentRunner, run_all_benchmarks
from experiments.scenarios import (
    build_bandwidth_sweep,
    build_baseline_suite,
    build_jitter_sweep,
    build_latency_sweep,
    build_loss_sweep,
    build_profiles_suite,
)
from experiments.visualizer import generate_all_figures
from metrics.collector import MetricsCollector
from network.conditions import NetworkCondition
from network.emulator import TCPEmulatorProxy, UDPEmulatorProxy
from network.profiles import PROFILES, get_profile
from server.tcp_server import TCPServer
from server.udp_server import UDPServer
from server.video_source import VideoSource


def cmd_server(args):
    """Start standalone streaming server."""
    source = VideoSource(width=args.width, height=args.height, fps=args.fps, jpeg_quality=args.quality)

    video_path = getattr(args, "video", None)
    if video_path:
        import os
        if not os.path.exists(video_path):
            print(f"[Error] Video file not found at path: {video_path}")
            sys.exit(1)
        stream_desc = f"custom video '{video_path}'"
    else:
        stream_desc = f"synthetic bouncing-ball ({args.frames} frames)"

    stream_gen = source.get_stream(video_path=video_path, total_frames=args.frames)
    target_fps = source.detected_fps if (source.detected_fps and not args.fps) else args.fps

    if args.protocol.upper() == "TCP":
        server = TCPServer(host=args.host, port=args.port)
        server.start()
        print(f"[TCP Server] Listening on {args.host}:{args.port}. Streaming {stream_desc} @ {target_fps} FPS...")
        server.stream_payloads(stream_gen, target_fps=target_fps)
        server.stop()
    else:
        server = UDPServer(target_host=args.target_host, target_port=args.port)
        server.start()
        print(f"[UDP Server] Streaming {stream_desc} @ {target_fps} FPS to {args.target_host}:{args.port}...")
        server.stream_payloads(stream_gen, target_fps=target_fps, max_chunk_size=1400)
        server.stop()


def cmd_client(args):
    """Start streaming client."""
    collector = MetricsCollector(protocol=args.protocol.upper())
    buffer = PlayoutBuffer(target_fps=args.fps, initial_buffer_frames=args.buffer_frames)
    renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=not args.gui, target_fps=args.fps)

    def on_frame(frame_id: int, payload: bytes, header):
        buffer.push_frame(frame_id, payload, header.timestamp_ns)

    renderer.start()

    if args.protocol.upper() == "TCP":
        client = TCPClient(server_host=args.host, server_port=args.port, metrics_collector=collector, on_frame_received=on_frame)
        print(f"[TCP Client] Connecting to {args.host}:{args.port} (GUI={args.gui})...")
        client.connect()
        client.receive_stream()
    else:
        client = UDPClient(listen_host=args.host, listen_port=args.port, metrics_collector=collector, on_frame_received=on_frame)
        client.start()
        print(f"[UDP Client] Listening on {args.host}:{args.port} (GUI={args.gui})...")
        client.receive_stream(max_duration_s=args.duration)

    buffer.mark_stream_end()
    while buffer.state != BufferState.FINISHED:
        time.sleep(0.05)

    renderer.stop()
    summary = collector.get_summary()
    stalls, stall_dur = buffer.get_stall_stats()
    summary["rebuffering_events"] = stalls
    summary["total_stall_seconds"] = round(stall_dur, 4)

    print("\n--- STREAM TELEMETRY SUMMARY ---")
    for k, v in summary.items():
        print(f"  {k:30s}: {v}")


def cmd_proxy(args):
    """Start standalone network impairment proxy."""
    if args.profile:
        cond = get_profile(args.profile)
    else:
        cond = NetworkCondition(
            name="CLI Custom Impairment",
            latency_ms=args.latency,
            jitter_ms=args.jitter,
            loss_percent=args.loss,
            bandwidth_mbps=args.bandwidth if args.bandwidth > 0 else None,
        )

    print(f"[Network Proxy] Mode: {args.protocol.upper()} | Listen: {args.listen_port} -> Target: {args.target_port}")
    print(f"  Delay: {cond.latency_ms}ms | Jitter: {cond.jitter_ms}ms | Loss: {cond.loss_percent}% | BW: {cond.bandwidth_mbps} Mbps")

    if args.protocol.upper() == "TCP":
        proxy = TCPEmulatorProxy(listen_port=args.listen_port, target_port=args.target_port, condition=cond)
    else:
        proxy = UDPEmulatorProxy(listen_port=args.listen_port, target_port=args.target_port, condition=cond)

    proxy.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping proxy...")
        proxy.stop()


def cmd_experiment(args):
    """Execute benchmark matrix sweeps."""
    runner = ExperimentRunner()
    frames = args.frames
    fps = args.fps

    suite_map = {
        "baseline": build_baseline_suite(total_frames=frames, fps=fps),
        "latency": build_latency_sweep(total_frames=frames, fps=fps),
        "loss": build_loss_sweep(total_frames=frames, fps=fps),
        "bandwidth": build_bandwidth_sweep(total_frames=frames, fps=fps),
        "jitter": build_jitter_sweep(total_frames=frames, fps=fps),
        "profiles": build_profiles_suite(total_frames=frames, fps=fps),
    }

    if args.suite.lower() == "all":
        run_all_benchmarks(total_frames=frames, fps=fps)
    elif args.suite.lower() in suite_map:
        suite = suite_map[args.suite.lower()]
        print(f"Executing suite '{args.suite}' ({len(suite)} scenarios)...")
        runner.run_suite(suite)
    else:
        print(f"Unknown suite '{args.suite}'. Available: all, baseline, latency, loss, bandwidth, jitter, profiles")

    # Generate figures after experiment run
    print("\nGenerating publication-grade comparative figures...")
    generate_all_figures()


def cmd_visualize(args):
    """Generate comparative plots from existing results CSV."""
    plots = generate_all_figures()
    print(f"Generated {len(plots)} comparison plots in experiments/results/plots/:")
    for p in plots:
        print(f"  - {p.name}")


def cmd_dashboard(args):
    """Launch FastAPI web dashboard."""
    import uvicorn
    from dashboard.app import app
    from dashboard.media import get_local_ip_addresses
    print(f"Starting StreamNet Dashboard on http://{args.host}:{args.port}")
    if args.host in ("0.0.0.0", "::"):
        ips = get_local_ip_addresses()
        print(f"Accessible on local machine: http://localhost:{args.port}")
        for ip in ips:
            print(f"Accessible over LAN:           http://{ip}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="StreamNet: TCP vs. UDP Real-Time Streaming Benchmark Suite")
    subparsers = parser.add_subparsers(dest="command", help="StreamNet Subcommands")

    # Server parser
    p_server = subparsers.add_parser("server", help="Run StreamNet Server")
    p_server.add_argument("--protocol", choices=["tcp", "udp"], default="tcp", help="Transport protocol")
    p_server.add_argument("--host", default="127.0.0.1", help="Bind host (TCP)")
    p_server.add_argument("--target-host", default="127.0.0.1", help="Target client host (UDP)")
    p_server.add_argument("--port", type=int, default=5001, help="Port")
    p_server.add_argument("--frames", type=int, default=150, help="Total frames to stream")
    p_server.add_argument("--fps", type=int, default=30, help="Target frame rate")
    p_server.add_argument("--width", type=int, default=640, help="Video width")
    p_server.add_argument("--height", type=int, default=480, help="Video height")
    p_server.add_argument("--quality", type=int, default=75, help="JPEG quality")
    p_server.add_argument("--video", type=str, default=None, help="Path to custom video file (e.g. sample.mp4). Falls back to synthetic stream if omitted.")

    # Client parser
    p_client = subparsers.add_parser("client", help="Run StreamNet Client")
    p_client.add_argument("--protocol", choices=["tcp", "udp"], default="tcp", help="Transport protocol")
    p_client.add_argument("--host", default="127.0.0.1", help="Server or Listen host")
    p_client.add_argument("--port", type=int, default=5001, help="Port")
    p_client.add_argument("--fps", type=int, default=30, help="Target playout FPS")
    p_client.add_argument("--buffer-frames", type=int, default=5, help="Initial playout buffer frames")
    p_client.add_argument("--duration", type=float, default=10.0, help="Max receive duration (UDP)")
    p_client.add_argument("--gui", action="store_true", help="Enable OpenCV GUI video display with telemetry HUD")

    # Proxy parser
    p_proxy = subparsers.add_parser("proxy", help="Run Standalone Network Impairment Proxy")
    p_proxy.add_argument("--protocol", choices=["tcp", "udp"], default="udp", help="Transport protocol")
    p_proxy.add_argument("--listen-port", type=int, default=6002, help="Proxy listen port")
    p_proxy.add_argument("--target-port", type=int, default=5002, help="Forwarding destination port")
    p_proxy.add_argument("--latency", type=float, default=0.0, help="Latency in ms")
    p_proxy.add_argument("--jitter", type=float, default=0.0, help="Jitter in ms")
    p_proxy.add_argument("--loss", type=float, default=0.0, help="Packet loss percentage")
    p_proxy.add_argument("--bandwidth", type=float, default=0.0, help="Bandwidth cap in Mbps (0 for unlimited)")
    p_proxy.add_argument("--profile", type=str, default=None, help="Use predefined profile name")

    # Experiment parser
    p_exp = subparsers.add_parser("experiment", help="Run automated benchmark experiment matrix")
    p_exp.add_argument("--suite", default="all", choices=["all", "baseline", "latency", "loss", "bandwidth", "jitter", "profiles"], help="Suite to run")
    p_exp.add_argument("--frames", type=int, default=90, help="Frames per trial")
    p_exp.add_argument("--fps", type=int, default=30, help="Stream FPS")

    # Visualize parser
    subparsers.add_parser("visualize", help="Generate comparative figures from results")

    # Dashboard parser
    p_dash = subparsers.add_parser("dashboard", help="Launch interactive web dashboard")
    p_dash.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0 for LAN access)")
    p_dash.add_argument("--port", type=int, default=8000, help="Port")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmd_map = {
        "server": cmd_server,
        "client": cmd_client,
        "proxy": cmd_proxy,
        "experiment": cmd_experiment,
        "visualize": cmd_visualize,
        "dashboard": cmd_dashboard,
    }

    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
