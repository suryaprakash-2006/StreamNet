"""
Unit and integration tests for Video Streaming components (VideoSource, PlayoutBuffer, Playback).
"""

import os
import threading
import time
import cv2
import numpy as np
import pytest

from client.buffer import BufferState, PlayoutBuffer
from client.playback import PlaybackRenderer
from client.tcp_client import TCPClient
from client.udp_client import UDPClient
from metrics.collector import MetricsCollector
from server.tcp_server import TCPServer
from server.udp_server import UDPServer
from server.video_source import VideoSource


def test_video_source_generation():
    """Verify synthetic video frame generator produces valid JPEG frames."""
    source = VideoSource(width=320, height=240, fps=30, jpeg_quality=70)
    frames = list(source.generate_synthetic_stream(total_frames=5))

    assert len(frames) == 5
    for frame_id, payload, flags in frames:
        assert isinstance(payload, bytes)
        assert len(payload) > 100
        # Decode JPEG bytes to verify integrity
        np_arr = np.frombuffer(payload, dtype=np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        assert img is not None
        assert img.shape == (240, 320, 3)


def test_playout_buffer_state_transitions():
    """Verify playout buffer lifecycle and stall duration tracking."""
    buf = PlayoutBuffer(target_fps=30.0, initial_buffer_frames=3, rebuffering_threshold=2)

    assert buf.state == BufferState.INITIAL_BUFFERING
    assert buf.pop_frame() is None

    # Push 2 frames (under initial threshold of 3)
    buf.push_frame(0, b"frame0", 1000)
    buf.push_frame(1, b"frame1", 2000)
    assert buf.state == BufferState.INITIAL_BUFFERING

    # Push 3rd frame -> reaches initial threshold -> transitions to PLAYING
    buf.push_frame(2, b"frame2", 3000)
    assert buf.state == BufferState.PLAYING

    # Pop 3 frames
    f0 = buf.pop_frame()
    assert f0 is not None and f0[0] == 0
    f1 = buf.pop_frame()
    assert f1 is not None and f1[0] == 1
    f2 = buf.pop_frame()
    assert f2 is not None and f2[0] == 2

    # Buffer is now empty while stream is still active -> triggers REBUFFERING
    assert buf.pop_frame() is None
    assert buf.state == BufferState.REBUFFERING
    assert buf.stall_events == 1

    # Push frames to reach rebuffering threshold (2 frames)
    buf.push_frame(3, b"frame3", 4000)
    assert buf.state == BufferState.REBUFFERING
    buf.push_frame(4, b"frame4", 5000)
    assert buf.state == BufferState.PLAYING

    buf.mark_stream_end()
    f3 = buf.pop_frame()
    assert f3 is not None and f3[0] == 3
    f4 = buf.pop_frame()
    assert f4 is not None and f4[0] == 4

    # Now drained and stream marked ended -> FINISHED
    assert buf.pop_frame() is None
    assert buf.state == BufferState.FINISHED


def test_tcp_video_streaming_integration():
    """Verify end-to-end TCP video streaming with synthetic video frames."""
    port = 5201
    total_frames = 15
    source = VideoSource(width=160, height=120, fps=60, jpeg_quality=50)

    server = TCPServer(host="127.0.0.1", port=port)
    server.start()

    collector = MetricsCollector(protocol="TCP")
    buffer = PlayoutBuffer(target_fps=60.0, initial_buffer_frames=2)
    renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=True, target_fps=60.0)

    def on_frame(frame_id: int, payload: bytes, header):
        buffer.push_frame(frame_id, payload, header.timestamp_ns)

    client = TCPClient(server_host="127.0.0.1", server_port=port, metrics_collector=collector, on_frame_received=on_frame)

    client_thread = threading.Thread(target=lambda: (time.sleep(0.05), client.connect(), client.receive_stream()))
    client_thread.start()

    renderer.start()

    server.stream_payloads(source.generate_synthetic_stream(total_frames=total_frames), target_fps=60.0)
    buffer.mark_stream_end()

    client_thread.join(timeout=3.0)
    t_drain = time.time() + 1.0
    while renderer.frames_rendered < total_frames and time.time() < t_drain:
        time.sleep(0.02)
    renderer.stop()
    server.stop()

    summary = collector.get_summary()
    assert summary["packets_received"] == total_frames
    assert renderer.frames_rendered == total_frames
    assert renderer.corrupted_frames == 0


def test_udp_video_streaming_integration():
    """Verify end-to-end UDP video streaming with synthetic video frames and MTU fragmentation."""
    port = 5202
    total_frames = 15
    source = VideoSource(width=320, height=240, fps=60, jpeg_quality=60)

    collector = MetricsCollector(protocol="UDP")
    buffer = PlayoutBuffer(target_fps=60.0, initial_buffer_frames=2)
    renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=True, target_fps=60.0)

    def on_frame(frame_id: int, payload: bytes, header):
        buffer.push_frame(frame_id, payload, header.timestamp_ns)

    client = UDPClient(listen_host="127.0.0.1", listen_port=port, metrics_collector=collector, on_frame_received=on_frame)
    client.start()

    client_thread = threading.Thread(target=lambda: client.receive_stream(max_duration_s=2.0))
    client_thread.start()

    renderer.start()

    time.sleep(0.05)
    server = UDPServer(target_host="127.0.0.1", target_port=port)
    server.start()

    server.stream_payloads(source.generate_synthetic_stream(total_frames=total_frames), target_fps=60.0, max_chunk_size=1400)
    buffer.mark_stream_end()

    client_thread.join(timeout=3.0)
    renderer.stop()
    server.stop()
    client.close()

    summary = collector.get_summary(total_packets_sent=server.packets_sent)
    assert summary["frames_completed"] == total_frames
    assert renderer.frames_rendered == total_frames
    assert renderer.corrupted_frames == 0


def test_read_video_file_valid(tmp_path):
    """Verify VideoSource reads and resizes real video frames from MP4 file."""
    sample_mp4 = str(tmp_path / "sample.mp4")
    VideoSource.create_sample_video(sample_mp4, duration_s=0.5, fps=30, width=640, height=480)

    source = VideoSource(width=320, height=240, fps=30, jpeg_quality=65)
    frames = list(source.read_video_file(sample_mp4))

    assert len(frames) == 15
    assert source.detected_fps == 30.0
    assert source.detected_frame_count == 15

    for frame_id, payload, flags in frames:
        assert isinstance(payload, bytes)
        assert len(payload) > 50
        np_arr = np.frombuffer(payload, dtype=np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        assert img is not None
        assert img.shape == (240, 320, 3)


def test_read_video_file_missing_path():
    """Verify VideoSource raises FileNotFoundError for missing path."""
    source = VideoSource()
    with pytest.raises(FileNotFoundError):
        list(source.read_video_file("nonexistent_video_path_12345.mp4"))


def test_read_video_file_eof_and_max_frames(tmp_path):
    """Verify clean EOF handling and max_frames capping."""
    sample_mp4 = str(tmp_path / "sample_short.mp4")
    VideoSource.create_sample_video(sample_mp4, duration_s=0.3, fps=30, width=320, height=240)  # 9 frames

    source = VideoSource(width=320, height=240, fps=30)

    # When max_frames=5 (less than 9 frames)
    frames_capped = list(source.read_video_file(sample_mp4, max_frames=5))
    assert len(frames_capped) == 5

    # When max_frames=50 (more than 9 frames -> stops at EOF)
    frames_eof = list(source.read_video_file(sample_mp4, max_frames=50))
    assert len(frames_eof) == 9

    # When video_path is None, get_stream falls back to synthetic stream
    fallback_frames = list(source.get_stream(video_path=None, total_frames=4))
    assert len(fallback_frames) == 4


def test_tcp_custom_video_streaming(tmp_path):
    """Verify end-to-end TCP streaming of a custom MP4 file."""
    sample_mp4 = str(tmp_path / "sample_tcp.mp4")
    VideoSource.create_sample_video(sample_mp4, duration_s=0.3, fps=30, width=320, height=240)  # 9 frames

    port = 5211
    source = VideoSource(width=160, height=120, fps=30, jpeg_quality=50)

    server = TCPServer(host="127.0.0.1", port=port)
    server.start()

    collector = MetricsCollector(protocol="TCP")
    buffer = PlayoutBuffer(target_fps=30.0, initial_buffer_frames=2)
    renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=True, target_fps=30.0)

    def on_frame(frame_id: int, payload: bytes, header):
        buffer.push_frame(frame_id, payload, header.timestamp_ns)

    client = TCPClient(server_host="127.0.0.1", server_port=port, metrics_collector=collector, on_frame_received=on_frame)
    client_thread = threading.Thread(target=lambda: (time.sleep(0.05), client.connect(), client.receive_stream()))
    client_thread.start()
    renderer.start()

    server.stream_payloads(source.get_stream(video_path=sample_mp4), target_fps=30.0)
    buffer.mark_stream_end()

    client_thread.join(timeout=3.0)
    renderer.stop()
    server.stop()

    summary = collector.get_summary()
    assert summary["packets_received"] == 9
    assert renderer.frames_rendered == 9
    assert renderer.corrupted_frames == 0


def test_udp_custom_video_streaming(tmp_path):
    """Verify end-to-end UDP streaming of a custom MP4 file with MTU chunking."""
    sample_mp4 = str(tmp_path / "sample_udp.mp4")
    VideoSource.create_sample_video(sample_mp4, duration_s=0.3, fps=30, width=320, height=240)  # 9 frames

    port = 5212
    source = VideoSource(width=320, height=240, fps=30, jpeg_quality=60)

    collector = MetricsCollector(protocol="UDP")
    buffer = PlayoutBuffer(target_fps=30.0, initial_buffer_frames=2)
    renderer = PlaybackRenderer(playout_buffer=buffer, metrics_collector=collector, headless=True, target_fps=30.0)

    def on_frame(frame_id: int, payload: bytes, header):
        buffer.push_frame(frame_id, payload, header.timestamp_ns)

    client = UDPClient(listen_host="127.0.0.1", listen_port=port, metrics_collector=collector, on_frame_received=on_frame)
    client.start()

    client_thread = threading.Thread(target=lambda: client.receive_stream(max_duration_s=2.0))
    client_thread.start()
    renderer.start()

    time.sleep(0.05)
    server = UDPServer(target_host="127.0.0.1", target_port=port)
    server.start()

    server.stream_payloads(source.get_stream(video_path=sample_mp4), target_fps=30.0, max_chunk_size=1400)
    buffer.mark_stream_end()

    client_thread.join(timeout=3.0)
    renderer.stop()
    server.stop()
    client.close()

    summary = collector.get_summary(total_packets_sent=server.packets_sent)
    assert summary["frames_completed"] == 9
    assert renderer.frames_rendered == 9
    assert renderer.corrupted_frames == 0


def test_playback_renderer_draw_hud_regression():
    """Regression test: verify _draw_hud executes cleanly without NameError across various image dimensions."""
    collector = MetricsCollector(protocol="TCP")
    buf = PlayoutBuffer()
    renderer = PlaybackRenderer(playout_buffer=buf, metrics_collector=collector, headless=True)

    # Test standard resolution (640x480)
    img_480p = np.zeros((480, 640, 3), dtype=np.uint8)
    renderer._draw_hud(img_480p, current_frame_id=12)
    assert img_480p.shape == (480, 640, 3)
    # Verify HUD pixels were drawn into bottom region
    assert np.any(img_480p[-100:, :] > 0)

    # Test medium resolution (320x240)
    img_240p = np.zeros((240, 320, 3), dtype=np.uint8)
    renderer._draw_hud(img_240p, current_frame_id=5)
    assert img_240p.shape == (240, 320, 3)
    assert np.any(img_240p[-60:, :] > 0)

    # Test low resolution (160x120)
    img_120p = np.zeros((120, 160, 3), dtype=np.uint8)
    renderer._draw_hud(img_120p, current_frame_id=1)
    assert img_120p.shape == (120, 160, 3)
    assert np.any(img_120p[-40:, :] > 0)

    # Test tiny edge-case dimensions (should return safely without crashing)
    img_tiny = np.zeros((15, 15, 3), dtype=np.uint8)
    renderer._draw_hud(img_tiny, current_frame_id=0)
    assert img_tiny.shape == (15, 15, 3)
