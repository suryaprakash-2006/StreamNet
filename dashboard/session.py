"""
StreamNet Dashboard Live Streaming Session Manager.

Orchestrates background TCP and UDP streaming sessions under controlled network impairments,
bridges client-received frames to browser-compatible MJPEG streams, and provides live,
unsimulated telemetry feeds.
"""

from dataclasses import dataclass, field
import asyncio
import enum
import logging
import math
import os
import queue
import socket
import threading
import time
from typing import Any, AsyncGenerator, Dict, Generator, Optional, Tuple

from client.audio_buffer import AudioBufferState, AudioPlayoutBuffer
from client.buffer import BufferState, PlayoutBuffer
from client.tcp_client import TCPClient
from client.udp_client import UDPClient
from common.protocol import HeaderFlags, MsgType
from metrics.collector import MetricsCollector
from network.conditions import NetworkCondition
from network.emulator import TCPEmulatorProxy, UDPEmulatorProxy
from server.audio_source import AudioSource, generate_wav_header
from server.tcp_server import TCPServer
from server.udp_server import UDPServer
from server.video_source import VideoSource

logger = logging.getLogger("StreamNet.DashboardSession")


def _find_available_port(start_port: int, host: str = "127.0.0.1") -> int:
    """Find the next available TCP port on host starting from start_port."""
    port = start_port
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                port += 1
    return start_port


def _find_available_udp_port(start_port: int, host: str = "127.0.0.1") -> int:
    """Find the next available UDP port on host starting from start_port."""
    port = start_port
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                port += 1
    return start_port


class SessionState(enum.Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    STREAMING = "STREAMING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass
class SessionConfig:
    protocol: str = "TCP"                  # "TCP" or "UDP"
    media_mode: str = "video"              # "video", "audio", or "audio_video"
    video_source_type: str = "synthetic"   # "synthetic" or "custom"
    video_path: Optional[str] = None       # e.g. "sample.mp4"
    audio_source_type: str = "synthetic"   # "synthetic", "custom", or "mic"
    audio_path: Optional[str] = None       # e.g. "sample.wav"
    audio_sample_rate: int = 16000         # 16000, 24000, 44100
    audio_channels: int = 1                # 1 (mono) or 2 (stereo)
    audio_tone_freq: float = 440.0         # 440 Hz
    audio_pattern: str = "sine"            # "sine" or "ping"
    audio_chunk_ms: float = 20.0           # Audio chunk duration (e.g. 20ms)
    total_frames: int = 150                # Total frames to stream (or 0 for full file)
    fps: int = 30                          # Target playout rate
    width: int = 640                       # Video width
    height: int = 480                      # Video height
    jpeg_quality: int = 75                 # JPEG encoding quality
    condition: NetworkCondition = field(default_factory=NetworkCondition)
    # Dedicated dashboard ports to avoid conflicts with CLI or benchmarks
    tcp_server_port: int = 5301
    tcp_proxy_port: int = 6301
    udp_client_port: int = 5302
    udp_proxy_port: int = 6302


class LiveStreamingSession:
    """
    Manages an active real-time TCP or UDP streaming session under controlled network degradation.
    Bridges received frames to a thread-safe browser queue and exposes live telemetry.
    """

    def __init__(self, config: SessionConfig):
        self.config = config
        self.protocol = config.protocol.upper()
        self.state = SessionState.IDLE
        self.error_message: Optional[str] = None

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stream_finished = threading.Event()

        # Telemetry and Video Playout components
        self.metrics = MetricsCollector(protocol=self.protocol)
        self.buffer = PlayoutBuffer(
            target_fps=config.fps,
            initial_buffer_frames=3,
            rebuffering_threshold=2,
        )

        # Thread-safe frame queue for browser MJPEG streaming (bounded to prevent lag)
        self.frame_queue: queue.Queue[Tuple[int, bytes]] = queue.Queue(maxsize=15)
        self.bridge_frames_delivered = 0
        self.bridge_frames_dropped = 0
        self._last_frame_payload: Optional[bytes] = None

        # Audio Components and Telemetry
        self.audio_source = AudioSource(
            sample_rate=config.audio_sample_rate,
            channels=config.audio_channels,
            chunk_duration_ms=getattr(config, "audio_chunk_ms", 20.0),
        )
        self.audio_buffer = AudioPlayoutBuffer(
            sample_rate=config.audio_sample_rate,
            channels=config.audio_channels,
            initial_buffer_ms=60.0,
            rebuffering_threshold_ms=40.0,
        )
        self.audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=150)
        self.audio_chunks_sent = 0
        self.audio_bytes_sent = 0
        self.audio_chunks_delivered = 0
        self.audio_bytes_delivered = 0
        self.audio_chunks_dropped = 0
        self._mic_queue: queue.Queue[bytes] = queue.Queue(maxsize=100)

        # Timing & FPS tracking
        self.session_start_time: Optional[float] = None
        self._frame_count_window = 0
        self._last_fps_calc_time = 0.0
        self.current_fps = 0.0

        # Background components
        self.server: Optional[Any] = None
        self.client: Optional[Any] = None
        self.proxy: Optional[Any] = None
        self._server_thread: Optional[threading.Thread] = None
        self._client_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start server, impairment proxy, client, and frame delivery pipeline."""
        with self._lock:
            if self.state in (SessionState.STARTING, SessionState.STREAMING):
                raise RuntimeError("Session is already active.")

            self.state = SessionState.STARTING
            self.error_message = None
            self._stop_event.clear()
            self._stream_finished.clear()

        try:
            # 1. Validate video input if custom file requested
            if self.config.video_source_type == "custom" and self.config.media_mode in ("video", "audio_video"):
                if not self.config.video_path:
                    raise ValueError("No video file path provided.")
                if not os.path.exists(self.config.video_path):
                    raise FileNotFoundError(f"Video file not found at: {self.config.video_path}")

            # Validate audio input if custom file requested
            if self.config.audio_source_type == "custom" and self.config.media_mode in ("audio", "audio_video"):
                if not self.config.audio_path:
                    raise ValueError("No audio file path provided.")
                if not os.path.exists(self.config.audio_path):
                    raise FileNotFoundError(f"Audio file not found at: {self.config.audio_path}")

            # 2. Initialize Video & Audio Sources
            video_source = VideoSource(
                width=self.config.width,
                height=self.config.height,
                fps=self.config.fps,
                jpeg_quality=self.config.jpeg_quality,
            )
            self.audio_source = AudioSource(
                sample_rate=self.config.audio_sample_rate,
                channels=self.config.audio_channels,
                chunk_duration_ms=20.0,
            )

            # 3. Setup Protocol-Specific Transport Pipeline
            if self.protocol == "TCP":
                self._start_tcp_pipeline(video_source)
            elif self.protocol == "UDP":
                self._start_udp_pipeline(video_source)
            else:
                raise ValueError(f"Unsupported protocol: {self.protocol}")

            self.session_start_time = time.monotonic()
            self._last_fps_calc_time = self.session_start_time
            self.state = SessionState.STREAMING
            logger.info("Live streaming session started successfully for protocol %s (mode: %s)", self.protocol, self.config.media_mode)

        except Exception as err:
            self.state = SessionState.ERROR
            self.error_message = str(err)
            logger.error("Failed to start live streaming session: %s", err)
            self.stop()
            raise

    def _create_stream_generator(self, video_source: VideoSource):
        """Creates appropriate payload stream generator based on media_mode."""
        mode = self.config.media_mode
        total_frames = self.config.total_frames if self.config.total_frames > 0 else None

        if mode == "video":
            return video_source.get_stream(
                video_path=self.config.video_path if self.config.video_source_type == "custom" else None,
                total_frames=total_frames,
            )

        elif mode == "audio":
            if self.config.audio_source_type == "mic":
                def mic_gen():
                    chunk_id = 0
                    while not self._stop_event.is_set():
                        try:
                            pcm = self._mic_queue.get(timeout=0.1)
                            self.audio_chunks_sent += 1
                            self.audio_bytes_sent += len(pcm)
                            yield (chunk_id, pcm, HeaderFlags.NONE, MsgType.AUDIO_FRAME)
                            chunk_id += 1
                        except queue.Empty:
                            if self._stream_finished.is_set():
                                break
                            continue
                return mic_gen()
            else:
                # 20ms chunks -> 50 chunks per second
                total_chunks = (total_frames * 2) if total_frames else 300
                def audio_gen():
                    for chunk_idx, payload, flags, msg_type in self.audio_source.get_stream(
                        audio_path=self.config.audio_path if self.config.audio_source_type == "custom" else None,
                        total_chunks=total_chunks,
                        tone_freq=self.config.audio_tone_freq,
                        pattern=self.config.audio_pattern,
                    ):
                        self.audio_chunks_sent += 1
                        self.audio_bytes_sent += len(payload)
                        yield (chunk_idx, payload, flags, msg_type)
                return audio_gen()

        else:
            # Synchronized audio_video multiplexed stream
            v_gen = video_source.get_stream(
                video_path=self.config.video_path if self.config.video_source_type == "custom" else None,
                total_frames=total_frames,
            )
            v_fps = max(1.0, float(self.config.fps))
            chunk_ms = float(getattr(self.config, "audio_chunk_ms", 20.0))
            v_frame_dur = 1.0 / v_fps
            a_chunk_dur = chunk_ms / 1000.0

            if self.config.audio_source_type == "mic":
                def multiplexed_mic_gen():
                    chunk_id = 0
                    for v_item in v_gen:
                        # Flush any queued microphone chunks before the video frame
                        while not self._mic_queue.empty():
                            try:
                                pcm = self._mic_queue.get_nowait()
                                self.audio_chunks_sent += 1
                                self.audio_bytes_sent += len(pcm)
                                yield (chunk_id, pcm, HeaderFlags.NONE, MsgType.AUDIO_FRAME)
                                chunk_id += 1
                            except queue.Empty:
                                break
                        yield v_item
                return multiplexed_mic_gen()
            else:
                # Dynamic audio accumulator ensures exact audio/video synchronization
                # and zero long-term clock drift between audio chunks and video frames.
                total_chunks = int(math.ceil((total_frames / v_fps) / a_chunk_dur) + 20) if total_frames else None
                a_gen = self.audio_source.get_stream(
                    audio_path=self.config.audio_path if self.config.audio_source_type == "custom" else None,
                    total_chunks=total_chunks,
                    tone_freq=self.config.audio_tone_freq,
                    pattern=self.config.audio_pattern,
                )

                def multiplexed_gen():
                    a_iter = iter(a_gen)
                    accumulated_audio_s = 0.0

                    for frame_idx, v_item in enumerate(v_gen):
                        target_audio_s = (frame_idx + 1) * v_frame_dur
                        while accumulated_audio_s < target_audio_s:
                            try:
                                a_chunk = next(a_iter)
                                self.audio_chunks_sent += 1
                                self.audio_bytes_sent += len(a_chunk[1])
                                yield a_chunk
                                accumulated_audio_s += a_chunk_dur
                            except StopIteration:
                                break
                        yield v_item

                return multiplexed_gen()

    def _start_tcp_pipeline(self, source: VideoSource) -> None:
        """Initialize TCP Server, Proxy, and Client."""
        srv_port = _find_available_port(self.config.tcp_server_port)
        prx_port = _find_available_port(self.config.tcp_proxy_port)
        target_client_port = srv_port

        # A. Start TCP Server
        self.server = TCPServer(host="127.0.0.1", port=srv_port)
        self.server.start()

        # B. Start Proxy if network condition is degraded
        if not self.config.condition.is_ideal():
            self.proxy = TCPEmulatorProxy(
                listen_host="127.0.0.1",
                listen_port=prx_port,
                target_host="127.0.0.1",
                target_port=srv_port,
                condition=self.config.condition,
            )
            self.proxy.start()
            target_client_port = prx_port
            time.sleep(0.05)

        # C. Setup Client Frame Handlers
        def on_tcp_frame(frame_id: int, payload: bytes, header: Any) -> None:
            self._handle_received_frame(frame_id, payload, header.timestamp_ns)

        def on_tcp_audio(chunk_id: int, payload: bytes, header: Any) -> None:
            self._handle_received_audio(chunk_id, payload, header.timestamp_ns)

        self.client = TCPClient(
            server_host="127.0.0.1",
            server_port=target_client_port,
            metrics_collector=self.metrics,
            on_frame_received=on_tcp_frame,
            on_audio_received=on_tcp_audio,
        )

        # D. Start Client in Background Thread
        def client_worker() -> None:
            time.sleep(0.05)
            if self._stop_event.is_set() or not self.client:
                return
            try:
                self.client.connect()
                self.client.receive_stream()
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error("TCP Client worker error: %s", e)
            finally:
                self.buffer.mark_stream_end()
                self.buffer.state = BufferState.FINISHED
                self.audio_buffer.mark_stream_end()
                self.audio_buffer.state = AudioBufferState.FINISHED
                self._stream_finished.set()

        self._client_thread = threading.Thread(target=client_worker, daemon=True)
        self._client_thread.start()

        # E. Start Server Streaming in Background Thread
        def server_worker() -> None:
            time.sleep(0.1)
            if self._stop_event.is_set() or not self.server:
                return
            try:
                stream_gen = self._create_stream_generator(source)
                if self.config.media_mode == "audio":
                    pacing_fps = 1000.0 / max(1.0, float(getattr(self.config, "audio_chunk_ms", 20.0)))
                    pace_audio = True
                elif self.config.media_mode == "audio_video":
                    pacing_fps = float(self.config.fps)
                    pace_audio = False
                else:
                    pacing_fps = float(self.config.fps)
                    pace_audio = True
                self.server.stream_payloads(stream_gen, target_fps=pacing_fps, pace_audio=pace_audio)
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error("TCP Server worker error: %s", e)
            finally:
                time.sleep(0.5)

        self._server_thread = threading.Thread(target=server_worker, daemon=True)
        self._server_thread.start()

    def _start_udp_pipeline(self, source: VideoSource) -> None:
        """Initialize UDP Client, Proxy, and Server."""
        cli_port = _find_available_udp_port(self.config.udp_client_port)
        prx_port = _find_available_udp_port(self.config.udp_proxy_port)
        target_server_port = cli_port

        # A. Setup Client Frame Handler & Start UDP Client
        def on_udp_frame(frame_id: int, payload: bytes, header: Any) -> None:
            self._handle_received_frame(frame_id, payload, header.timestamp_ns)

        def on_udp_audio(chunk_id: int, payload: bytes, header: Any) -> None:
            self._handle_received_audio(chunk_id, payload, header.timestamp_ns)

        self.client = UDPClient(
            listen_host="127.0.0.1",
            listen_port=cli_port,
            metrics_collector=self.metrics,
            on_frame_received=on_udp_frame,
            on_audio_received=on_udp_audio,
            socket_timeout_s=1.5,
        )
        self.client.start()

        # B. Start Client Receiver Thread
        def client_worker() -> None:
            if self._stop_event.is_set() or not self.client:
                return
            try:
                self.client.receive_stream()
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error("UDP Client worker error: %s", e)
            finally:
                self.buffer.mark_stream_end()
                self.buffer.state = BufferState.FINISHED
                self.audio_buffer.mark_stream_end()
                self.audio_buffer.state = AudioBufferState.FINISHED
                self._stream_finished.set()

        self._client_thread = threading.Thread(target=client_worker, daemon=True)
        self._client_thread.start()

        # C. Start Proxy if degraded
        if not self.config.condition.is_ideal():
            self.proxy = UDPEmulatorProxy(
                listen_host="127.0.0.1",
                listen_port=prx_port,
                target_host="127.0.0.1",
                target_port=cli_port,
                condition=self.config.condition,
            )
            self.proxy.start()
            target_server_port = prx_port
            time.sleep(0.05)

        # D. Start UDP Server
        self.server = UDPServer(target_host="127.0.0.1", target_port=target_server_port)
        self.server.start()

        def server_worker() -> None:
            time.sleep(0.1)
            if self._stop_event.is_set() or not self.server:
                return
            try:
                stream_gen = self._create_stream_generator(source)
                if self.config.media_mode == "audio":
                    pacing_fps = 1000.0 / max(1.0, float(getattr(self.config, "audio_chunk_ms", 20.0)))
                    pace_audio = True
                elif self.config.media_mode == "audio_video":
                    pacing_fps = float(self.config.fps)
                    pace_audio = False
                else:
                    pacing_fps = float(self.config.fps)
                    pace_audio = True
                self.server.stream_payloads(
                    stream_gen,
                    target_fps=pacing_fps,
                    max_chunk_size=1400,
                    pace_audio=pace_audio,
                )
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.error("UDP Server worker error: %s", e)
            finally:
                time.sleep(0.5)

        self._server_thread = threading.Thread(target=server_worker, daemon=True)
        self._server_thread.start()

    def _handle_received_frame(self, frame_id: int, payload: bytes, timestamp_ns: int) -> None:
        """Push assembled frame to playout buffer and browser MJPEG queue."""
        self.buffer.push_frame(frame_id, payload, timestamp_ns)
        self._last_frame_payload = payload

        # Update instantaneous FPS
        self._frame_count_window += 1
        now = time.monotonic()
        dt = now - self._last_fps_calc_time
        if dt >= 1.0:
            self.current_fps = round(self._frame_count_window / dt, 1)
            self._frame_count_window = 0
            self._last_fps_calc_time = now

        # Forward to browser queue with bounding
        try:
            self.frame_queue.put_nowait((frame_id, payload))
            self.bridge_frames_delivered += 1
        except queue.Full:
            # Drop oldest frame to ensure real-time latency
            try:
                self.frame_queue.get_nowait()
                self.bridge_frames_dropped += 1
                self.frame_queue.put_nowait((frame_id, payload))
            except (queue.Empty, queue.Full):
                pass

    def _handle_received_audio(self, chunk_id: int, payload: bytes, timestamp_ns: int) -> None:
        """Push assembled audio chunk to audio playout buffer and browser audio feed queue."""
        self.audio_buffer.push_chunk(chunk_id, payload, timestamp_ns)
        self.audio_chunks_delivered += 1
        self.audio_bytes_delivered += len(payload)

        # Forward to browser audio streaming queue (bounded)
        try:
            self.audio_queue.put_nowait(payload)
        except queue.Full:
            try:
                self.audio_queue.get_nowait()
                self.audio_chunks_dropped += 1
                self.audio_queue.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass

    def push_mic_audio(self, pcm_bytes: bytes) -> None:
        """Inject real-time microphone PCM bytes captured from browser."""
        try:
            self._mic_queue.put_nowait(pcm_bytes)
        except queue.Full:
            try:
                self._mic_queue.get_nowait()
                self._mic_queue.put_nowait(pcm_bytes)
            except (queue.Empty, queue.Full):
                pass

    def generate_mjpeg_stream(self) -> Generator[bytes, None, None]:
        """
        Yields multipart/x-mixed-replace JPEG frames to the browser.
        """
        while not self._stop_event.is_set():
            try:
                frame_id, payload = self.frame_queue.get(timeout=0.2)
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                    + payload
                    + b"\r\n"
                )
            except queue.Empty:
                if (self._stream_finished.is_set() or self.buffer.state == BufferState.FINISHED) and self.frame_queue.empty():
                    logger.info("Stream finished and frame queue drained; ending MJPEG stream.")
                    break
                continue
            except Exception as e:
                logger.warning("Error generating MJPEG frame: %s", e)
                break

    async def generate_mjpeg_stream_async(self) -> AsyncGenerator[bytes, None]:
        """
        Asynchronously yields multipart/x-mixed-replace JPEG frames to the browser without blocking the event loop.
        """
        while not self._stop_event.is_set():
            try:
                frame_id, payload = self.frame_queue.get_nowait()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                    + payload
                    + b"\r\n"
                )
            except queue.Empty:
                if (self._stream_finished.is_set() or self.buffer.state == BufferState.FINISHED) and self.frame_queue.empty():
                    logger.info("Buffer finished and frame queue drained; ending async MJPEG stream.")
                    break
                await asyncio.sleep(0.005)
            except Exception as e:
                logger.warning("Error in async MJPEG generator: %s", e)
                break

    async def generate_audio_stream_async(self) -> AsyncGenerator[bytes, None]:
        """
        Yields standard streaming WAV header followed by live uncompressed PCM chunks
        delivered across the transport sockets.
        """
        # Yield 44-byte WAV header configured for streaming
        header = generate_wav_header(
            sample_rate=self.config.audio_sample_rate,
            channels=self.config.audio_channels,
            bits_per_sample=16,
            total_data_bytes=0x7FFFFFFF,
        )
        yield header

        while not self._stop_event.is_set():
            try:
                chunk = self.audio_queue.get_nowait()
                yield chunk
            except queue.Empty:
                if (self._stream_finished.is_set() or self.audio_buffer.state == AudioBufferState.FINISHED) and self.audio_queue.empty():
                    break
                await asyncio.sleep(0.01)
            except Exception as e:
                logger.warning("Error in audio stream generator: %s", e)
                break

    def get_telemetry(self) -> Dict[str, Any]:
        """
        Extract live, non-simulated performance metrics directly from the active transport pipeline.
        """
        with self._lock:
            # 1. Base network summary
            sent_packets = getattr(self.server, "packets_sent", None)
            if self.protocol == "TCP":
                sent_packets = getattr(self.server, "frames_sent", None)

            summary = self.metrics.get_summary(total_packets_sent=sent_packets)
            stalls, stall_dur = self.buffer.get_stall_stats()

            frames_sent = getattr(self.server, "frames_sent", 0) if self.server else 0
            frames_completed = summary.get("frames_completed", 0)

            # In UDP, dropped frames occur when chunks fail reassembly
            frames_lost = max(0, frames_sent - frames_completed) if frames_sent > 0 else 0
            fdr = round((frames_completed / max(1, frames_sent)) * 100.0, 1) if frames_sent > 0 else 100.0

            # Instantaneous latency from latest received packet
            instant_lat = self.metrics.latencies_ms[-1] if self.metrics.latencies_ms else 0.0

            # Genuine audio metrics
            duration = summary.get("duration_seconds", 0.0)
            audio_bitrate_kbps = 0.0
            if duration > 0 and self.audio_bytes_delivered > 0:
                audio_bitrate_kbps = (self.audio_bytes_delivered * 8.0) / (duration * 1000.0)

            audio_stalls, audio_stall_dur = self.audio_buffer.get_stall_stats()
            audio_lost = max(0, self.audio_chunks_sent - self.audio_chunks_delivered) if self.audio_chunks_sent > 0 else 0
            audio_loss_pct = (audio_lost / max(1, self.audio_chunks_sent)) * 100.0 if self.audio_chunks_sent > 0 else 0.0

            is_audio_active = self.config.media_mode in ("audio", "audio_video")

            return {
                "session_state": self.state.value,
                "protocol": self.protocol,
                "media_mode": self.config.media_mode,
                "duration_seconds": summary.get("duration_seconds", 0.0),
                # Bandwidth & Goodput
                "throughput_mbps": summary.get("throughput_mbps", 0.0),
                "goodput_mbps": summary.get("goodput_mbps", 0.0),
                # Latency & Jitter
                "latency_mean_ms": summary.get("latency_mean_ms", 0.0),
                "latency_instant_ms": round(instant_lat, 2),
                "rfc3550_jitter_ms": summary.get("rfc3550_jitter_ms", 0.0),
                # Loss & Integrity
                "packet_loss_rate_percent": summary.get("packet_loss_rate_percent", 0.0),
                "packets_sent": summary.get("packets_sent", 0),
                "packets_received": summary.get("packets_received", 0),
                "packets_lost": summary.get("packets_lost", 0),
                # Frame & Playout
                "frames_sent": frames_sent,
                "frames_completed": frames_completed,
                "frames_lost": frames_lost,
                "frame_delivery_ratio_percent": fdr,
                "current_fps": self.current_fps,
                # Playout Buffer State
                "buffer_state": self.buffer.state.value,
                "buffer_depth_ms": round(self.buffer.get_buffer_depth_ms(), 1),
                "rebuffering_events": stalls,
                "total_stall_seconds": round(stall_dur, 2),
                # Browser Bridge Metrics (distinguishes transport from HTTP delivery)
                "bridge_frames_delivered": self.bridge_frames_delivered,
                "bridge_frames_dropped": self.bridge_frames_dropped,
                "bridge_queue_size": self.frame_queue.qsize(),
                # Active Network Condition
                "condition": {
                    "latency_ms": self.config.condition.latency_ms,
                    "jitter_ms": self.config.condition.jitter_ms,
                    "loss_percent": self.config.condition.loss_percent,
                    "bandwidth_mbps": self.config.condition.bandwidth_mbps,
                },
                # Video & Limitations
                "video_source": {
                    "type": self.config.video_source_type,
                    "path": self.config.video_path or "Synthetic Stream",
                    "width": self.config.width,
                    "height": self.config.height,
                    "audio_supported": is_audio_active,
                    "audio_notice": (
                        f"Active Audio Stream ({self.config.audio_source_type}, {self.config.audio_sample_rate}Hz)"
                        if is_audio_active
                        else "Audio tracks are not transmitted in video-only mode."
                    ),
                },
                # Audio Telemetry
                "audio": {
                    "enabled": is_audio_active,
                    "mode": self.config.media_mode,
                    "source_type": self.config.audio_source_type,
                    "sample_rate": self.config.audio_sample_rate,
                    "channels": self.config.audio_channels,
                    "chunks_sent": self.audio_chunks_sent,
                    "chunks_received": self.audio_chunks_delivered,
                    "bytes_sent": self.audio_bytes_sent,
                    "bytes_delivered": self.audio_bytes_delivered,
                    "bitrate_kbps": round(audio_bitrate_kbps, 1),
                    "buffer_depth_ms": round(self.audio_buffer.get_buffer_depth_ms(), 1),
                    "buffer_state": self.audio_buffer.state.value,
                    "underruns": audio_stalls,
                    "stall_duration_seconds": round(audio_stall_dur, 2),
                    "packets_lost": audio_lost,
                    "loss_percent": round(audio_loss_pct, 1),
                    "audio_bridge_queue": self.audio_queue.qsize(),
                },
            }

    def stop(self) -> None:
        """Cleanly terminate background pipeline, release sockets, and clear queues."""
        with self._lock:
            self._stop_event.set()
            self._stream_finished.set()
            if self.state != SessionState.ERROR:
                self.state = SessionState.STOPPED

        # 1. Stop Client
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

        # 2. Stop Server
        if self.server:
            try:
                self.server.stop()
            except Exception:
                pass
            self.server = None

        # 3. Stop Proxy
        if self.proxy:
            try:
                self.proxy.stop()
            except Exception:
                pass
            self.proxy = None

        # 4. Join threads
        if self._client_thread and self._client_thread.is_alive():
            self._client_thread.join(timeout=1.0)
            self._client_thread = None

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=1.0)
            self._server_thread = None

        logger.info("Live streaming session fully stopped and resources released.")


class SessionManager:
    """Singleton session coordinator ensuring safe single-session execution."""

    def __init__(self):
        self._current_session: Optional[LiveStreamingSession] = None
        self._lock = threading.Lock()

    def get_current_session(self) -> Optional[LiveStreamingSession]:
        with self._lock:
            return self._current_session

    def start_session(self, config: SessionConfig) -> LiveStreamingSession:
        with self._lock:
            if self._current_session is not None:
                logger.info("Stopping previous active session before launching new stream.")
                self._current_session.stop()
                self._current_session = None
                time.sleep(0.15)

            session = LiveStreamingSession(config)
            session.start()
            self._current_session = session
            return session

    def stop_session(self) -> None:
        with self._lock:
            if self._current_session is not None:
                self._current_session.stop()
                self._current_session = None


# Global session manager instance
session_manager = SessionManager()
