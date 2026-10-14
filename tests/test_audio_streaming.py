import asyncio
import io
import time
import wave
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from common.protocol import MsgType, HeaderFlags
from server.audio_source import AudioSource, generate_wav_header
from server.video_source import VideoSource
from client.audio_buffer import AudioPlayoutBuffer, AudioBufferState
from dashboard.app import app
from dashboard.session import session_manager, SessionConfig, LiveStreamingSession


@pytest.fixture(autouse=True)
def clean_live_session():
    """Ensure session manager is cleaned up before and after each test."""
    session_manager.stop_session()
    time.sleep(0.15)
    yield
    session_manager.stop_session()
    time.sleep(0.15)



def test_audio_source_synthetic_sine():
    """Verify synthetic sine tone generation producing standard 16kHz 16-bit mono PCM."""
    src = AudioSource(sample_rate=16000, channels=1, chunk_duration_ms=20.0)
    gen = src.generate_synthetic_stream(total_chunks=10, tone_freq=440.0, pattern="sine")
    frames = list(gen)
    assert len(frames) == 10
    
    # 20ms at 16000Hz = 320 samples * 2 bytes/sample = 640 bytes
    for fid, pcm, flags, mtype in frames:
        assert isinstance(fid, int)
        assert len(pcm) == 640
        assert mtype == MsgType.AUDIO_FRAME
    
    # Verify last frame has LAST_CHUNK flag
    assert frames[-1][2] & HeaderFlags.LAST_CHUNK != 0


def test_audio_source_synthetic_ping():
    """Verify synthetic acoustic ping pulse generation."""
    src = AudioSource(sample_rate=16000, channels=1, chunk_duration_ms=20.0)
    gen = src.generate_synthetic_stream(total_chunks=5, tone_freq=880.0, pattern="ping")
    frames = list(gen)
    assert len(frames) == 5
    assert all(len(pcm) == 640 for _, pcm, _, _ in frames)


def test_audio_source_wav_file(tmp_path: Path):
    """Verify reading frames from a real WAV file and handling EOF cleanly."""
    wav_file = tmp_path / "test_tone.wav"
    AudioSource.create_sample_wav(str(wav_file), duration_s=0.2, sample_rate=16000)
    assert wav_file.exists()

    src = AudioSource(sample_rate=16000, channels=1, chunk_duration_ms=20.0)
    gen = src.read_wav_file(str(wav_file))
    frames = list(gen)
    # 0.2s / 0.02s = 10 frames
    assert len(frames) == 10
    assert frames[0][3] == MsgType.AUDIO_FRAME


def test_audio_source_missing_wav_file():
    """Verify error handling when custom audio path does not exist."""
    src = AudioSource()
    with pytest.raises(FileNotFoundError):
        list(src.read_wav_file("nonexistent_audio_file.wav"))


def test_audio_wav_header_generation():
    """Verify standard 44-byte WAV header generator validity."""
    header = generate_wav_header(sample_rate=16000, channels=1, bits_per_sample=16, total_data_bytes=6400)
    assert len(header) == 44
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    assert header[12:16] == b"fmt "
    assert header[36:40] == b"data"

    # Parse with standard library wave module to verify standards compliance
    with wave.open(io.BytesIO(header + b"\x00" * 6400), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2


def test_audio_playout_buffer_lifecycle():
    """Verify jitter buffer queuing, playout state transitions, underruns, and depth calculation."""
    # 40ms target = 40 * 16 * 2 = 1280 bytes
    buf = AudioPlayoutBuffer(
        sample_rate=16000,
        channels=1,
        bits_per_sample=16,
        initial_buffer_ms=40.0,
        rebuffering_threshold_ms=40.0,
    )
    assert buf.state == AudioBufferState.INITIAL_BUFFERING
    assert buf.get_buffer_depth_ms() == 0.0

    # Insert 1 frame (20ms = 640 bytes) - below 40ms threshold
    sample_chunk = b"\x00" * 640
    now_ns = time.time_ns()
    buf.push_chunk(0, sample_chunk, now_ns)
    assert buf.get_buffer_depth_ms() == 20.0
    assert buf.state == AudioBufferState.INITIAL_BUFFERING

    # Attempting to pop during INITIAL_BUFFERING returns None
    assert buf.pop_chunk() is None

    # Insert 2nd frame (40ms total) - reaches initial buffer threshold -> transitions to PLAYING
    buf.push_chunk(1, sample_chunk, now_ns)
    assert buf.get_buffer_depth_ms() == 40.0
    assert buf.state == AudioBufferState.PLAYING

    # Pop 1 frame - retrieves chunk (chunk_id, payload, timestamp_ns)
    res = buf.pop_chunk()
    assert res is not None
    chunk_id, pcm, _ = res
    assert chunk_id == 0
    assert pcm == sample_chunk
    assert buf.state == AudioBufferState.PLAYING
    assert buf.get_buffer_depth_ms() == 20.0

    # Pop 2nd frame - empties the queue
    res2 = buf.pop_chunk()
    assert res2 is not None
    assert res2[0] == 1
    assert buf.get_buffer_depth_ms() == 0.0

    # Pop when empty during playback -> triggers REBUFFERING and registers stall
    empty_pop = buf.pop_chunk()
    assert empty_pop is None
    assert buf.state == AudioBufferState.REBUFFERING
    stalls, _ = buf.get_stall_stats()
    assert stalls >= 1

    # Mark stream end transitions buffer to FINISHED when queue is drained
    buf.mark_stream_end()
    assert buf.state in (AudioBufferState.PLAYING, AudioBufferState.FINISHED)
    buf.pop_chunk()
    assert buf.state == AudioBufferState.FINISHED


def test_live_dashboard_audio_streaming_lifecycle():
    """Verify live TCP audio streaming session, audio-feed route, and SSE telemetry."""
    client = TestClient(app)

    # 1. Start audio-only live session
    start_payload = {
        "protocol": "TCP",
        "media_mode": "audio",
        "audio_source_type": "synthetic",
        "audio_pattern": "sine",
        "total_frames": 30,
        "fps": 50,
        "latency_ms": 0.0,
        "loss_percent": 0.0,
    }

    res = client.post("/api/live/start", json=start_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "started"
    assert data["media_mode"] == "audio"

    # 2. Check telemetry endpoint returns non-simulated active audio metrics
    time.sleep(0.3)
    status_res = client.get("/api/live/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["session_state"] in ("STREAMING", "STOPPED")
    assert status_data["media_mode"] == "audio"
    assert "audio" in status_data
    assert status_data["audio"]["enabled"] is True
    assert status_data["audio"]["source_type"] == "synthetic"
    assert status_data["audio"]["sample_rate"] == 16000

    # 3. Request audio feed
    feed_res = client.get("/api/live/audio-feed")
    assert feed_res.status_code == 200
    assert feed_res.headers["content-type"] == "audio/wav"
    # Initial bytes must be valid 44-byte WAV header
    header_bytes = feed_res.iter_bytes()
    initial_chunk = next(header_bytes)
    assert len(initial_chunk) >= 44
    assert initial_chunk[:4] == b"RIFF"
    assert initial_chunk[8:12] == b"WAVE"

    # 4. Stop session
    stop_res = client.post("/api/live/stop")
    assert stop_res.status_code == 200
    assert stop_res.json()["status"] == "stopped"


def test_live_dashboard_synced_av_streaming():
    """Verify synchronized audio+video mode multiplexed over UDP under network loss."""
    client = TestClient(app)

    start_payload = {
        "protocol": "UDP",
        "media_mode": "audio_video",
        "video_source_type": "synthetic",
        "audio_source_type": "synthetic",
        "audio_pattern": "ping",
        "total_frames": 25,
        "fps": 30,
        "latency_ms": 10.0,
        "loss_percent": 2.0,
        "jitter_ms": 5.0,
    }

    res = client.post("/api/live/start", json=start_payload)
    assert res.status_code == 200
    assert res.json()["media_mode"] == "audio_video"

    time.sleep(0.4)
    status_res = client.get("/api/live/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["media_mode"] == "audio_video"
    assert status_data["audio"]["enabled"] is True

    # Both video feed and audio feed must be available
    vfeed = client.get("/api/live/video-feed")
    assert vfeed.status_code == 200

    afeed = client.get("/api/live/audio-feed")
    assert afeed.status_code == 200

    stop_res = client.post("/api/live/stop")
    assert stop_res.status_code == 200


def test_live_dashboard_microphone_injection():
    """Verify real-time microphone PCM upload endpoint."""
    client = TestClient(app)

    # Start mic audio session
    start_payload = {
        "protocol": "TCP",
        "media_mode": "audio",
        "audio_source_type": "mic",
        "total_frames": 100,
        "fps": 50,
    }

    res = client.post("/api/live/start", json=start_payload)
    assert res.status_code == 200

    # Inject 1600 samples (3200 bytes = 100ms) of PCM data
    dummy_mic_pcm = b"\x00\x05" * 1600
    upload_res = client.post(
        "/api/live/mic-upload",
        content=dummy_mic_pcm,
        headers={"Content-Type": "application/octet-stream"}
    )
    assert upload_res.status_code == 200
    data = upload_res.json()
    assert data["status"] == "ok"
    assert data["bytes_received"] == 3200

    stop_res = client.post("/api/live/stop")
    assert stop_res.status_code == 200


def test_audio_playout_buffer_out_of_order_reordering_and_stale_drop():
    """Verify jitter buffer sorts out-of-order UDP packets and discards stale late arrivals."""
    buf = AudioPlayoutBuffer(
        sample_rate=16000,
        channels=1,
        bits_per_sample=16,
        initial_buffer_ms=40.0,
    )
    sample_chunk = b"\x01\x00" * 320  # 640 bytes = 20ms
    now = time.time_ns()

    # Push chunk 0
    buf.push_chunk(0, sample_chunk, now)
    # Push chunk 2 (arrived before chunk 1 due to UDP jitter)
    buf.push_chunk(2, sample_chunk, now + 40000000)
    # Push chunk 1 (arrived late)
    buf.push_chunk(1, sample_chunk, now + 20000000)

    # 3 chunks of 20ms = 60ms >= initial_buffer_ms (40ms) -> PLAYING
    assert buf.state == AudioBufferState.PLAYING
    assert buf.get_buffer_depth_ms() == 60.0

    # Pop 1: must be chunk 0
    item0 = buf.pop_chunk()
    assert item0 is not None and item0[0] == 0

    # Pop 2: must be chunk 1 (sorted in-order, NOT 2)
    item1 = buf.pop_chunk()
    assert item1 is not None and item1[0] == 1

    # Pop 3: must be chunk 2
    item2 = buf.pop_chunk()
    assert item2 is not None and item2[0] == 2

    # Verify stale chunk rejection: chunk 0 or 1 arriving now after playout has passed
    buf.push_chunk(0, sample_chunk, now)
    buf.push_chunk(1, sample_chunk, now)
    # Buffer should not accept stale packets; depth remains 0.0
    assert buf.get_buffer_depth_ms() == 0.0


def test_live_dashboard_synced_av_mic_mode():
    """Verify synchronized audio+video mode with microphone audio source."""
    client = TestClient(app)

    start_payload = {
        "protocol": "TCP",
        "media_mode": "audio_video",
        "video_source_type": "synthetic",
        "audio_source_type": "mic",
        "total_frames": 20,
        "fps": 30,
    }

    res = client.post("/api/live/start", json=start_payload)
    assert res.status_code == 200
    assert res.json()["media_mode"] == "audio_video"

    # Inject microphone PCM bytes
    dummy_mic = b"\x10\x00" * 640  # 2 chunks worth
    upload_res = client.post(
        "/api/live/mic-upload",
        content=dummy_mic,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert upload_res.status_code == 200

    time.sleep(0.3)
    status_res = client.get("/api/live/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["media_mode"] == "audio_video"
    assert status_data["audio"]["enabled"] is True
    assert status_data["audio"]["source_type"] == "mic"

    stop_res = client.post("/api/live/stop")
    assert stop_res.status_code == 200


def test_synchronized_av_generator_pacing_and_ratio():
    """Verify dynamic interleaving ratio: 30 video frames (1.0s at 30fps) yield 50 audio chunks (1.0s at 20ms)."""
    config = SessionConfig(
        protocol="TCP",
        media_mode="audio_video",
        video_source_type="synthetic",
        audio_source_type="synthetic",
        audio_pattern="sine",
        audio_chunk_ms=20.0,
        total_frames=30,
        fps=30,
    )
    session = LiveStreamingSession(config)
    v_source = VideoSource(width=160, height=120, fps=30)
    gen = session._create_stream_generator(v_source)

    items = list(gen)
    video_items = [it for it in items if len(it) == 3 or (len(it) == 4 and it[3] == MsgType.DATA_FRAME)]
    audio_items = [it for it in items if len(it) == 4 and it[3] == MsgType.AUDIO_FRAME]

    # Exactly 30 video frames
    assert len(video_items) == 30
    # Exactly 50 audio chunks (1000ms / 20ms = 50 chunks for 1000ms video)
    assert len(audio_items) == 50

    # Verify progressive interleaving: first item yielded is an audio chunk
    assert items[0][3] == MsgType.AUDIO_FRAME

