"""
StreamNet Audio Playout and Jitter Buffer.

Maintains an audio playout buffer that absorbs packet jitter, monitors underrun/stalls,
and tracks audio buffer depth in milliseconds for real-time telemetry.
"""

import bisect
import enum
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("StreamNet.AudioBuffer")


class AudioBufferState(enum.Enum):
    """Audio buffer lifecycle states."""
    INITIAL_BUFFERING = "INITIAL_BUFFERING"
    PLAYING = "PLAYING"
    REBUFFERING = "REBUFFERING"
    FINISHED = "FINISHED"


class AudioPlayoutBuffer:
    """
    Thread-safe Playout / Jitter Buffer for audio PCM chunks.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        bits_per_sample: int = 16,
        initial_buffer_ms: float = 60.0,
        rebuffering_threshold_ms: float = 40.0,
        max_buffer_ms: float = 2000.0,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bits_per_sample = bits_per_sample
        self.bytes_per_sample = channels * (bits_per_sample // 8)
        self.bytes_per_second = sample_rate * self.bytes_per_sample

        self.initial_buffer_bytes = int((initial_buffer_ms / 1000.0) * self.bytes_per_second)
        self.rebuffering_threshold_bytes = int((rebuffering_threshold_ms / 1000.0) * self.bytes_per_second)
        self.max_buffer_bytes = int((max_buffer_ms / 1000.0) * self.bytes_per_second)

        self._lock = threading.Lock()
        self._chunks: Dict[int, Tuple[bytes, int]] = {}  # chunk_id -> (payload, timestamp_ns)
        self._chunk_queue: List[int] = []  # sorted list of queued chunk_ids
        self._total_queued_bytes = 0
        self._last_popped_chunk_id: Optional[int] = None

        self.state = AudioBufferState.INITIAL_BUFFERING
        self._stream_ended = False

        # Metrics
        self.chunks_received = 0
        self.bytes_received = 0
        self.underrun_events = 0
        self.total_stall_duration_s = 0.0
        self._stall_start_time: Optional[float] = None

    def push_chunk(self, chunk_id: int, payload: bytes, timestamp_ns: int) -> None:
        """Add an assembled audio PCM chunk to the playout buffer."""
        with self._lock:
            # Reject stale chunks that arrived after their playout sequence
            if self._last_popped_chunk_id is not None and chunk_id <= self._last_popped_chunk_id:
                logger.debug(
                    "Dropping stale out-of-order audio chunk %d (last popped: %d)",
                    chunk_id,
                    self._last_popped_chunk_id,
                )
                return

            if chunk_id in self._chunks:
                return  # Duplicate chunk, ignore

            self._chunks[chunk_id] = (payload, timestamp_ns)
            bisect.insort(self._chunk_queue, chunk_id)
            self._total_queued_bytes += len(payload)
            self.chunks_received += 1
            self.bytes_received += len(payload)

            # Drop oldest chunks if buffer exceeds max limit
            while self._total_queued_bytes > self.max_buffer_bytes and len(self._chunk_queue) > 1:
                dropped_id = self._chunk_queue.pop(0)
                dropped_payload, _ = self._chunks.pop(dropped_id, (b"", 0))
                self._total_queued_bytes -= len(dropped_payload)

            # Check state transitions
            if self.state == AudioBufferState.INITIAL_BUFFERING:
                if self._total_queued_bytes >= self.initial_buffer_bytes or self._stream_ended:
                    self.state = AudioBufferState.PLAYING
                    logger.debug("Audio buffer primed (%d bytes). State -> PLAYING.", self._total_queued_bytes)

            elif self.state == AudioBufferState.REBUFFERING:
                if self._total_queued_bytes >= self.rebuffering_threshold_bytes or self._stream_ended:
                    if self._stall_start_time is not None:
                        stall_dur = time.monotonic() - self._stall_start_time
                        self.total_stall_duration_s += stall_dur
                        self._stall_start_time = None
                    self.state = AudioBufferState.PLAYING
                    logger.debug("Audio buffer refilled. State -> PLAYING.")

    def mark_stream_end(self) -> None:
        """Signal that server finished transmitting audio."""
        with self._lock:
            self._stream_ended = True
            if self.state in (AudioBufferState.INITIAL_BUFFERING, AudioBufferState.REBUFFERING):
                self.state = AudioBufferState.PLAYING

    def pop_chunk(self) -> Optional[Tuple[int, bytes, int]]:
        """
        Retrieve the next ordered audio chunk.
        Returns (chunk_id, pcm_bytes, timestamp_ns) or None if buffering or empty.
        """
        with self._lock:
            if self.state == AudioBufferState.INITIAL_BUFFERING:
                return None

            if len(self._chunk_queue) == 0:
                if self._stream_ended:
                    self.state = AudioBufferState.FINISHED
                    return None
                elif self.state == AudioBufferState.PLAYING:
                    self.state = AudioBufferState.REBUFFERING
                    self.underrun_events += 1
                    self._stall_start_time = time.monotonic()
                    logger.debug("Audio buffer underrun. State -> REBUFFERING.")
                return None

            chunk_id = self._chunk_queue.pop(0)
            payload, timestamp_ns = self._chunks.pop(chunk_id)
            self._total_queued_bytes -= len(payload)
            if chunk_id >= 0:
                self._last_popped_chunk_id = chunk_id
            return (chunk_id, payload, timestamp_ns)

    def read_pcm_bytes(self, max_bytes: int) -> bytes:
        """
        Drain up to max_bytes of audio PCM data from the queued chunks.
        Useful for continuous playback streaming or audio device feeds.
        """
        out = bytearray()
        while len(out) < max_bytes:
            item = self.pop_chunk()
            if item is None:
                break
            _, chunk_bytes, _ = item
            needed = max_bytes - len(out)
            if len(chunk_bytes) <= needed:
                out.extend(chunk_bytes)
            else:
                out.extend(chunk_bytes[:needed])
                # Put remainder back at the front of the queue
                remainder = chunk_bytes[needed:]
                with self._lock:
                    synthetic_id = -1
                    self._chunks[synthetic_id] = (remainder, 0)
                    self._chunk_queue.insert(0, synthetic_id)
                    self._total_queued_bytes += len(remainder)
                break
        return bytes(out)

    def get_buffer_depth_ms(self) -> float:
        """Return current buffer occupancy in milliseconds of audio playtime."""
        with self._lock:
            if self.bytes_per_second <= 0:
                return 0.0
            return (self._total_queued_bytes / float(self.bytes_per_second)) * 1000.0

    def get_stall_stats(self) -> Tuple[int, float]:
        """Return (underrun_events, total_stall_duration_s)."""
        with self._lock:
            total_dur = self.total_stall_duration_s
            if self._stall_start_time is not None:
                total_dur += (time.monotonic() - self._stall_start_time)
            return (self.underrun_events, total_dur)
