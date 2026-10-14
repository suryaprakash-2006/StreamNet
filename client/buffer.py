"""
StreamNet Playout / Jitter Buffer.

Maintains a playout queue and state machine (Buffering -> Playing -> Rebuffering -> Finished)
to simulate real-world media playback and measure rebuffering events and stall durations.
"""

from collections import deque
import enum
import logging
import threading
import time
from typing import Deque, Dict, Optional, Tuple

logger = logging.getLogger("StreamNet.PlayoutBuffer")


class BufferState(enum.Enum):
    """Buffer playback lifecycle states."""
    INITIAL_BUFFERING = "INITIAL_BUFFERING"
    PLAYING = "PLAYING"
    REBUFFERING = "REBUFFERING"
    FINISHED = "FINISHED"


class PlayoutBuffer:
    """
    Thread-safe Playout / Jitter Buffer for video frames.
    """

    def __init__(
        self,
        target_fps: float = 30.0,
        initial_buffer_frames: int = 5,
        rebuffering_threshold: int = 3,
        max_buffer_frames: int = 60,
    ):
        self.target_fps = target_fps
        self.frame_duration_s = 1.0 / target_fps if target_fps > 0 else 0.033
        self.initial_buffer_frames = initial_buffer_frames
        self.rebuffering_threshold = rebuffering_threshold
        self.max_buffer_frames = max_buffer_frames

        self._lock = threading.Lock()
        self._frames: Dict[int, Tuple[bytes, int]] = {}  # frame_id -> (payload, timestamp_ns)
        self._frame_queue: Deque[int] = deque()

        self.state = BufferState.INITIAL_BUFFERING
        self._next_expected_playout_frame_id = 0
        self._stream_ended = False

        # Stall metrics
        self.stall_events = 0
        self.total_stall_duration_s = 0.0
        self._stall_start_time: Optional[float] = None

    def push_frame(self, frame_id: int, payload: bytes, timestamp_ns: int) -> None:
        """Add a newly assembled frame to the buffer."""
        with self._lock:
            if frame_id in self._frames:
                return  # Ignore duplicates

            self._frames[frame_id] = (payload, timestamp_ns)
            self._frame_queue.append(frame_id)

            current_depth = len(self._frame_queue)

            # Check state transitions
            if self.state == BufferState.INITIAL_BUFFERING:
                if current_depth >= self.initial_buffer_frames or self._stream_ended:
                    self.state = BufferState.PLAYING
                    logger.debug("Buffer reached initial threshold (%d frames). Transitioning to PLAYING.", current_depth)

            elif self.state == BufferState.REBUFFERING:
                if current_depth >= self.rebuffering_threshold or self._stream_ended:
                    if self._stall_start_time is not None:
                        stall_dur = time.monotonic() - self._stall_start_time
                        self.total_stall_duration_s += stall_dur
                        self._stall_start_time = None
                    self.state = BufferState.PLAYING
                    logger.debug("Buffer refilled (%d frames). Resuming PLAYING.", current_depth)

    def mark_stream_end(self) -> None:
        """Signal that server finished transmitting all frames."""
        with self._lock:
            self._stream_ended = True
            if self.state == BufferState.INITIAL_BUFFERING or self.state == BufferState.REBUFFERING:
                self.state = BufferState.PLAYING

    def pop_frame(self) -> Optional[Tuple[int, bytes, int]]:
        """
        Retrieve next frame for rendering according to playback state.
        Returns (frame_id, payload, timestamp_ns) or None if stalled / buffering.
        """
        with self._lock:
            if self.state == BufferState.INITIAL_BUFFERING:
                return None

            if len(self._frame_queue) == 0:
                if self._stream_ended:
                    self.state = BufferState.FINISHED
                    return None
                elif self.state == BufferState.PLAYING:
                    # Enter Rebuffering state
                    self.state = BufferState.REBUFFERING
                    self.stall_events += 1
                    self._stall_start_time = time.monotonic()
                    logger.debug("Buffer underrun. Entering REBUFFERING state.")
                return None

            frame_id = self._frame_queue.popleft()
            payload, timestamp_ns = self._frames.pop(frame_id)
            return (frame_id, payload, timestamp_ns)

    def get_queue_size(self) -> int:
        """Return number of queued frames."""
        with self._lock:
            return len(self._frame_queue)

    def get_buffer_depth_ms(self) -> float:
        """Return current buffer occupancy in milliseconds of video time."""
        with self._lock:
            return len(self._frame_queue) * self.frame_duration_s * 1000.0

    def get_stall_stats(self) -> Tuple[int, float]:
        """Return (stall_events, total_stall_duration_s)."""
        with self._lock:
            total_dur = self.total_stall_duration_s
            if self._stall_start_time is not None:
                total_dur += (time.monotonic() - self._stall_start_time)
            return (self.stall_events, total_dur)
