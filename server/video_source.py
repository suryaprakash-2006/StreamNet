"""
StreamNet Video Source and Workload Generator.

Provides deterministic synthetic video frame generation with dynamic visual overlays
(frame counter, timestamps, smooth motion patterns) and standard video file ingestion via OpenCV.
"""

import math
import os
import time
from typing import Generator, Optional, Tuple
import cv2
import numpy as np

from common.protocol import HeaderFlags


class VideoSource:
    """
    Generates or loads video frames encoded as JPEG payloads.
    """

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        fps: int = 30,
        jpeg_quality: int = 75,
        gop_size: int = 30,
    ):
        self.width = width
        self.height = height
        self.fps = fps
        self.jpeg_quality = max(10, min(100, jpeg_quality))
        self.gop_size = gop_size
        self.detected_fps: Optional[float] = None
        self.detected_frame_count: Optional[int] = None

    def generate_synthetic_stream(
        self,
        total_frames: int = 300,
    ) -> Generator[Tuple[int, bytes, HeaderFlags], None, None]:
        """
        Generate a sequence of synthetic test video frames.
        Each frame contains:
          - A high-contrast background with smooth gradient/grid
          - A rotating color-wheel / bouncing motion indicator to reveal frame drops/stalls
          - Millisecond-precision generation timestamp & frame ID
          - StreamNet watermark & resolution banner
        """
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        start_time = time.time()

        for frame_idx in range(total_frames):
            # Create dark base canvas
            frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
            frame[:] = (20, 24, 30)  # Dark slate background

            # Draw background grid
            grid_spacing = 40
            for x in range(0, self.width, grid_spacing):
                cv2.line(frame, (x, 0), (x, self.height), (35, 40, 50), 1)
            for y in range(0, self.height, grid_spacing):
                cv2.line(frame, (0, y), (self.width, y), (35, 40, 50), 1)

            # Draw top banner
            cv2.rectangle(frame, (0, 0), (self.width, 50), (45, 52, 65), -1)
            cv2.putText(
                frame,
                "StreamNet - Real-Time Transport Evaluation",
                (15, 32),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 220, 255),  # Cyan
                2,
                cv2.LINE_AA,
            )

            # Frame ID and Timestamp Overlay
            elapsed_ms = (time.time() - start_time) * 1000.0
            info_text = f"Frame: {frame_idx:05d} / {total_frames:05d} | Time: {elapsed_ms:08.1f}ms | FPS: {self.fps}"
            cv2.putText(
                frame,
                info_text,
                (15, 85),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

            # Dynamic Bouncing Indicator (visualizes jitter / frame loss)
            center_x = int(self.width / 2 + (self.width * 0.35) * math.sin(frame_idx * 0.08))
            center_y = int(self.height / 2 + 60 + (self.height * 0.15) * math.cos(frame_idx * 0.12))
            ball_color = (
                int(127 + 127 * math.sin(frame_idx * 0.1)),
                int(127 + 127 * math.sin(frame_idx * 0.1 + 2)),
                int(127 + 127 * math.sin(frame_idx * 0.1 + 4)),
            )
            cv2.circle(frame, (center_x, center_y), 28, ball_color, -1)
            cv2.circle(frame, (center_x, center_y), 28, (255, 255, 255), 2)

            # Rotating Radar / Clock Hand in top right
            radar_center = (self.width - 70, 110)
            radar_radius = 40
            cv2.circle(frame, radar_center, radar_radius, (60, 70, 90), 2)
            angle_rad = (frame_idx * 12 % 360) * (math.pi / 180.0)
            hand_end = (
                int(radar_center[0] + radar_radius * math.cos(angle_rad)),
                int(radar_center[1] + radar_radius * math.sin(angle_rad)),
            )
            cv2.line(frame, radar_center, hand_end, (0, 255, 128), 2)

            # Bottom Status Bar
            cv2.rectangle(frame, (0, self.height - 35), (self.width, self.height), (30, 35, 45), -1)
            res_text = f"Resolution: {self.width}x{self.height} | Quality: {self.jpeg_quality}%"
            cv2.putText(
                frame,
                res_text,
                (15, self.height - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (180, 180, 180),
                1,
                cv2.LINE_AA,
            )

            # Encode frame to JPEG
            success, encoded_img = cv2.imencode(".jpg", frame, encode_params)
            if not success:
                raise RuntimeError(f"Failed to encode frame {frame_idx} to JPEG")

            payload_bytes = encoded_img.tobytes()

            # Determine keyframe flag
            flags = HeaderFlags.NONE
            if frame_idx % self.gop_size == 0:
                flags |= HeaderFlags.KEYFRAME
            if frame_idx == total_frames - 1:
                flags |= HeaderFlags.LAST_CHUNK

            yield (frame_idx, payload_bytes, flags)

    def read_video_file(
        self,
        video_path: str,
        max_frames: Optional[int] = None,
    ) -> Generator[Tuple[int, bytes, HeaderFlags], None, None]:
        """
        Read frames from a custom video file (e.g. MP4) and yield JPEG-encoded payloads.
        Handles file verification, resizing, and clean EOF termination.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found at path: {video_path}")

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Cannot open video source (unsupported format or corrupt file): {video_path}")

        native_fps = cap.get(cv2.CAP_PROP_FPS)
        if native_fps and native_fps > 0:
            self.detected_fps = float(native_fps)

        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if frame_count and frame_count > 0:
            self.detected_frame_count = int(frame_count)

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        frame_idx = 0

        try:
            while True:
                if max_frames is not None and frame_idx >= max_frames:
                    break

                ret, frame = cap.read()
                if not ret or frame is None:
                    # Clean End-of-File reached
                    break

                # Resize to target transport dimensions if needed
                if frame.shape[1] != self.width or frame.shape[0] != self.height:
                    frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)

                success, encoded_img = cv2.imencode(".jpg", frame, encode_params)
                if not success:
                    continue

                payload = encoded_img.tobytes()
                flags = HeaderFlags.KEYFRAME if frame_idx % self.gop_size == 0 else HeaderFlags.NONE
                if max_frames is not None and frame_idx == max_frames - 1:
                    flags |= HeaderFlags.LAST_CHUNK

                yield (frame_idx, payload, flags)
                frame_idx += 1
        finally:
            cap.release()

    def get_stream(
        self,
        video_path: Optional[str] = None,
        total_frames: Optional[int] = None,
    ) -> Generator[Tuple[int, bytes, HeaderFlags], None, None]:
        """
        Yields video stream from a custom video file if provided and valid;
        otherwise falls back seamlessly to the synthetic bouncing-ball test generator.
        """
        if video_path is not None and str(video_path).strip() != "":
            return self.read_video_file(str(video_path).strip(), max_frames=total_frames)
        return self.generate_synthetic_stream(total_frames=total_frames or 150)

    @staticmethod
    def create_sample_video(
        output_path: str,
        duration_s: float = 3.0,
        fps: int = 30,
        width: int = 640,
        height: int = 480,
    ) -> str:
        """
        Generates a valid standalone MP4 video file for testing and demos.
        """
        total_frames = max(1, int(duration_s * fps))
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        out = cv2.VideoWriter(output_path, fourcc, float(fps), (width, height))
        if not out.isOpened():
            raise RuntimeError(f"Failed to initialize VideoWriter for path: {output_path}")

        try:
            for i in range(total_frames):
                frame = np.zeros((height, width, 3), dtype=np.uint8)
                # Background color gradient
                frame[:, :, 0] = np.uint8((np.arange(width) / width) * 200)
                frame[:, :, 1] = np.uint8((np.arange(height)[:, np.newaxis] / height) * 150)
                frame[:, :, 2] = np.uint8(100 + 100 * math.sin(i * 0.1))

                # Dynamic moving indicator bar
                bar_x = int((i * 12) % width)
                cv2.rectangle(frame, (bar_x, 0), (min(width, bar_x + 35), height), (0, 255, 255), -1)

                # Text banner
                cv2.putText(
                    frame,
                    f"StreamNet Custom MP4 - Frame {i:04d}/{total_frames:04d}",
                    (25, height // 2),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
                out.write(frame)
        finally:
            out.release()

        return output_path
