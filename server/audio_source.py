"""
StreamNet Audio Source and Workload Generator.

Provides:
  - Deterministic synthetic audio generation (sine waves, acoustic radar chirp/ping)
  - Standard uncompressed PCM WAV file ingestion via Python's standard library wave module
  - Helpers for WAV header serialization for browser audio playback
"""

import math
import os
import struct
import time
from typing import Any, Dict, Generator, Optional, Tuple
import wave

from common.protocol import HeaderFlags, MsgType


def generate_wav_header(
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = 16,
    total_data_bytes: int = 0x7FFFFFFF,
) -> bytes:
    """
    Generate a 44-byte standard RIFF/WAVE header for streaming or file writing.
    Using total_data_bytes=0x7FFFFFFF creates an unbounded/streaming WAV header for live HTTP feeds.
    """
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    riff_chunk_size = min(0xFFFFFFFF, total_data_bytes + 36)

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_chunk_size,
        b"WAVE",
        b"fmt ",
        16,                 # Subchunk1Size (16 for PCM)
        1,                  # AudioFormat (1 for uncompressed PCM)
        channels,           # NumChannels
        sample_rate,        # SampleRate
        byte_rate,          # ByteRate
        block_align,        # BlockAlign
        bits_per_sample,    # BitsPerSample
        b"data",
        total_data_bytes,   # Subchunk2Size
    )
    return header


class AudioSource:
    """
    Generates or loads audio frames encoded as 16-bit signed PCM payloads.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_duration_ms: float = 20.0,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_duration_ms = chunk_duration_ms
        self.samples_per_chunk = max(1, int(self.sample_rate * (self.chunk_duration_ms / 1000.0)))
        self.bytes_per_sample = 2  # 16-bit PCM = 2 bytes per sample per channel
        self.bytes_per_chunk = self.samples_per_chunk * self.channels * self.bytes_per_sample

        self.detected_sample_rate: Optional[int] = None
        self.detected_channels: Optional[int] = None
        self.detected_duration_s: Optional[float] = None
        self.detected_total_frames: Optional[int] = None

    def generate_synthetic_stream(
        self,
        total_chunks: int = 150,
        tone_freq: float = 440.0,
        pattern: str = "sine",  # "sine" or "ping"
    ) -> Generator[Tuple[int, bytes, HeaderFlags, MsgType], None, None]:
        """
        Generates deterministic 16-bit signed PCM audio chunks.
        Pattern 'sine': Continuous sinusoidal pure tone at tone_freq (default 440Hz concert pitch).
        Pattern 'ping': Acoustic radar ping (short tone burst every 500ms followed by silence).
        """
        for chunk_idx in range(total_chunks):
            start_sample = chunk_idx * self.samples_per_chunk
            pcm_data = bytearray()

            for s_idx in range(self.samples_per_chunk):
                sample_num = start_sample + s_idx
                t = sample_num / float(self.sample_rate)

                if pattern == "ping":
                    # Burst active for 80ms every 500ms
                    cycle_time = t % 0.5
                    amplitude = 0.8 * math.exp(-cycle_time * 25.0) if cycle_time < 0.15 else 0.0
                    freq = 880.0
                    val = amplitude * math.sin(2.0 * math.pi * freq * t)
                else:
                    # Pure continuous sine wave
                    amplitude = 0.7
                    val = amplitude * math.sin(2.0 * math.pi * tone_freq * t)

                # Clamp to 16-bit signed range (-32768 to 32767)
                int_sample = int(max(-32768, min(32767, val * 32767.0)))
                packed_sample = struct.pack("<h", int_sample)

                # For stereo duplicate channel
                for _ in range(self.channels):
                    pcm_data.extend(packed_sample)

            payload = bytes(pcm_data)
            flags = HeaderFlags.KEYFRAME if chunk_idx % 20 == 0 else HeaderFlags.NONE
            if chunk_idx == total_chunks - 1:
                flags |= HeaderFlags.LAST_CHUNK

            yield (chunk_idx, payload, flags, MsgType.AUDIO_FRAME)

    def read_wav_file(
        self,
        file_path: str,
        max_chunks: Optional[int] = None,
    ) -> Generator[Tuple[int, bytes, HeaderFlags, MsgType], None, None]:
        """
        Reads real audio frames from an uncompressed PCM WAV file.
        Yields chunked frames matching the configured chunk size.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Audio file not found at: {file_path}")

        try:
            with wave.open(file_path, "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                total_frames = wf.getnframes()

                self.detected_channels = channels
                self.detected_sample_rate = sample_rate
                self.detected_total_frames = total_frames
                self.detected_duration_s = total_frames / float(sample_rate) if sample_rate > 0 else 0.0

                if sample_width != 2:
                    # Supported formats must be 16-bit PCM for native network transport
                    pass  # StreamNet standardizes on 16-bit PCM

                chunk_frames = max(1, int(sample_rate * (self.chunk_duration_ms / 1000.0)))
                chunk_idx = 0

                while True:
                    if max_chunks is not None and chunk_idx >= max_chunks:
                        break

                    raw_frames = wf.readframes(chunk_frames)
                    if not raw_frames:
                        break  # Clean EOF

                    flags = HeaderFlags.KEYFRAME if chunk_idx % 20 == 0 else HeaderFlags.NONE
                    if max_chunks is not None and chunk_idx == max_chunks - 1:
                        flags |= HeaderFlags.LAST_CHUNK

                    yield (chunk_idx, raw_frames, flags, MsgType.AUDIO_FRAME)
                    chunk_idx += 1

        except wave.Error as e:
            raise ValueError(f"Failed to read WAV file ({file_path}): {e}")

    def get_stream(
        self,
        audio_path: Optional[str] = None,
        total_chunks: Optional[int] = None,
        tone_freq: float = 440.0,
        pattern: str = "sine",
    ) -> Generator[Tuple[int, bytes, HeaderFlags, MsgType], None, None]:
        """
        Yields audio stream from custom WAV file if provided and exists;
        otherwise falls back to synthetic tone generator.
        """
        if audio_path is not None and str(audio_path).strip() != "":
            return self.read_wav_file(str(audio_path).strip(), max_chunks=total_chunks)
        return self.generate_synthetic_stream(
            total_chunks=total_chunks or 150,
            tone_freq=tone_freq,
            pattern=pattern,
        )

    @staticmethod
    def extract_wav_info(file_path: str) -> Dict[str, Any]:
        """Extract metadata from a WAV file."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        try:
            with wave.open(file_path, "rb") as wf:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()
                n_frames = wf.getnframes()
                duration = n_frames / float(sample_rate) if sample_rate > 0 else 0.0
                return {
                    "channels": channels,
                    "sample_width": sample_width,
                    "bits_per_sample": sample_width * 8,
                    "sample_rate": sample_rate,
                    "total_frames": n_frames,
                    "duration_seconds": round(duration, 2),
                    "valid_wav": True,
                }
        except Exception as e:
            return {
                "valid_wav": False,
                "error": str(e),
            }

    @staticmethod
    def create_sample_wav(
        output_path: str,
        duration_s: float = 2.0,
        sample_rate: int = 16000,
        freq: float = 440.0,
        channels: int = 1,
    ) -> str:
        """
        Create a valid standalone 16-bit PCM WAV file for tests and demonstrations.
        """
        total_samples = int(duration_s * sample_rate)
        with wave.open(output_path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)

            pcm_bytes = bytearray()
            for i in range(total_samples):
                t = i / float(sample_rate)
                val = 0.7 * math.sin(2.0 * math.pi * freq * t)
                int_val = int(max(-32768, min(32767, val * 32767.0)))
                packed = struct.pack("<h", int_val)
                for _ in range(channels):
                    pcm_bytes.extend(packed)

            wf.writeframes(bytes(pcm_bytes))

        return output_path
