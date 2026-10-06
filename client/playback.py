"""
StreamNet Playback Renderer and Video Display.

Consumes frames from the PlayoutBuffer, decodes JPEG payloads, renders live performance
telemetry HUD (protocol, buffer level, latency, loss, jitter), and displays video via OpenCV
or simulates playback in headless mode.
"""

import logging
import threading
import time
from typing import Optional
import cv2
import numpy as np

from client.buffer import BufferState, PlayoutBuffer
from metrics.collector import MetricsCollector

logger = logging.getLogger("StreamNet.Playback")


class PlaybackRenderer:
    """
    Renders video stream and tracks display-level metrics.
    """

    def __init__(
        self,
        playout_buffer: PlayoutBuffer,
        metrics_collector: MetricsCollector,
        headless: bool = True,
        window_title: str = "StreamNet Video Playback",
        target_fps: float = 30.0,
    ):
        self.buffer = playout_buffer
        self.metrics = metrics_collector
        self.headless = headless
        self.window_title = window_title
        self.target_fps = target_fps
        self.frame_interval_s = 1.0 / target_fps if target_fps > 0 else 0.033

        self.is_running = False
        self._thread: Optional[threading.Thread] = None

        self.frames_rendered = 0
        self.corrupted_frames = 0

    def start(self) -> None:
        """Start playback thread."""
        self.is_running = True
        self._thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._thread.start()

    def _playback_loop(self) -> None:
        """Main rendering and playout pacing loop."""
        logger.info("Playback loop started (headless=%s)", self.headless)

        while self.is_running:
            start_tick = time.monotonic()

            if self.buffer.state == BufferState.FINISHED:
                logger.info("Buffer state FINISHED. Playback complete.")
                break

            frame_data = self.buffer.pop_frame()
            if frame_data is None:
                # Buffer is buffering, rebuffering, or waiting for data
                time.sleep(0.005)
                continue

            frame_id, payload, send_time_ns = frame_data

            # Decode JPEG payload
            if payload:
                np_arr = np.frombuffer(payload, dtype=np.uint8)
                img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            else:
                img = None

            if img is None:
                self.corrupted_frames += 1
                logger.warning("Corrupted or incomplete frame payload for frame %d", frame_id)
            else:
                self.frames_rendered += 1

                if not self.headless:
                    # Render Telemetry HUD overlay
                    self._draw_hud(img, frame_id)
                    cv2.imshow(self.window_title, img)
                    key = cv2.waitKey(1) & 0xFF
                    if key == 27 or key == ord("q"):  # ESC or Q to quit
                        self.is_running = False
                        break

            # Playout pacing
            elapsed = time.monotonic() - start_tick
            sleep_time = self.frame_interval_s - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

        if not self.headless:
            try:
                cv2.destroyWindow(self.window_title)
            except Exception:
                pass

        logger.info("Playback ended. Rendered: %d frames, Corrupted: %d", self.frames_rendered, self.corrupted_frames)

    def _draw_hud(self, img: np.ndarray, current_frame_id: int) -> None:
        """Draw real-time network telemetry HUD over video frame."""
        h, w = img.shape[:2]
        hud_bg = np.zeros((100, w, 3), dtype=np.uint8)
        hud_bg[:] = (15, 18, 22)

        # Telemetry metrics
        proto = self.metrics.protocol
        buf_ms = self.buffer.get_buffer_depth_ms()
        jitter_ms = self.metrics.rfc3550_jitter_ms
        loss_rate = self.metrics.get_summary()["packet_loss_rate_percent"]
        stalls, stall_dur = self.buffer.get_stall_stats()

        summary_line_1 = f"PROTOCOL: {proto} | FRAME: #{current_frame_id:04d} | BUFFER: {buf_ms:05.1f}ms ({self.buffer.state.value})"
        summary_line_2 = f"LOSS: {loss_rate:04.1f}% | JITTER: {jitter_ms:04.2f}ms | STALLS: {stalls} ({stall_dur:04.2f}s)"

        cv2.putText(hud_bg, summary_line_1, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 200), 1, cv2.LINE_AA)
        cv2.putText(hud_bg, summary_line_2, (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 200, 100), 1, cv2.LINE_AA)

        # Blend HUD at bottom of image if space permits
        if h >= 200:
            img[-100:, :] = cv2.addWeighted(img[-100:, :], 0.2, hud_bg, 0.8, 0)

    def stop(self) -> None:
        """Stop playback loop."""
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
