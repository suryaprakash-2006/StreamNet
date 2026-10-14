"""
Tests for StreamNet Dashboard Live Streaming and Telemetry Endpoints.
"""

import json
import time
import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.session import session_manager, SessionState


@pytest.fixture(autouse=True)
def clean_live_session():
    """Ensure session manager is cleaned up before and after each test."""
    session_manager.stop_session()
    time.sleep(0.15)
    yield
    session_manager.stop_session()
    time.sleep(0.15)


def test_live_status_idle():
    """Verify status endpoint returns IDLE when no streaming session is active."""
    client = TestClient(app)
    response = client.get("/api/live/status")
    assert response.status_code == 200
    data = response.json()
    assert data.get("session_state") == "IDLE"


def test_live_video_feed_not_found_when_idle():
    """Verify video feed returns 404 when no session is active."""
    client = TestClient(app)
    response = client.get("/api/live/video-feed")
    assert response.status_code == 404
    assert "No active live streaming session" in response.json()["detail"]


def test_live_start_invalid_protocol():
    """Verify validation error when unsupported protocol is specified."""
    client = TestClient(app)
    payload = {
        "protocol": "SCTP",
        "video_source_type": "synthetic",
        "total_frames": 20,
    }
    response = client.post("/api/live/start", json=payload)
    assert response.status_code == 400
    assert "Protocol must be either 'TCP' or 'UDP'" in response.json()["detail"]


def test_live_start_missing_custom_file():
    """Verify 400 error when requested custom video file does not exist."""
    client = TestClient(app)
    payload = {
        "protocol": "TCP",
        "video_source_type": "custom",
        "video_path": "non_existent_streamnet_file_9999.mp4",
        "total_frames": 20,
    }
    response = client.post("/api/live/start", json=payload)
    assert response.status_code == 400
    assert "Custom video file not found" in response.json()["detail"]


def test_live_tcp_session_lifecycle():
    """Test start, status telemetry, video chunk delivery, and stop for TCP."""
    client = TestClient(app)
    payload = {
        "protocol": "TCP",
        "video_source_type": "synthetic",
        "total_frames": 12,
        "fps": 30,
        "width": 320,
        "height": 240,
        "jpeg_quality": 60,
        "latency_ms": 0.0,
        "loss_percent": 0.0,
    }
    # 1. Start
    res_start = client.post("/api/live/start", json=payload)
    assert res_start.status_code == 200
    data_start = res_start.json()
    assert data_start["status"] == "started"
    assert data_start["protocol"] == "TCP"

    # Give pipeline a moment to transmit and drain frames
    time.sleep(0.6)

    # 2. Status
    res_status = client.get("/api/live/status")
    assert res_status.status_code == 200
    telemetry = res_status.json()
    assert telemetry["protocol"] == "TCP"
    assert "throughput_mbps" in telemetry
    assert "latency_mean_ms" in telemetry
    assert "buffer_state" in telemetry
    assert "bridge_frames_delivered" in telemetry
    assert telemetry["bridge_frames_delivered"] > 0
    assert telemetry["video_source"]["audio_supported"] is False
    assert "Audio tracks are not transmitted" in telemetry["video_source"]["audio_notice"]

    # 3. Read video feed
    with client.stream("GET", "/api/live/video-feed") as res_feed:
        assert res_feed.status_code == 200
        assert "multipart/x-mixed-replace" in res_feed.headers.get("content-type", "")
        data = res_feed.read()
        assert len(data) > 0
        assert b"--frame" in data

    # 4. Stop
    res_stop = client.post("/api/live/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["status"] == "stopped"


def test_live_udp_session_with_impairment():
    """Test live UDP session with network impairment proxy."""
    client = TestClient(app)
    payload = {
        "protocol": "UDP",
        "video_source_type": "synthetic",
        "total_frames": 15,
        "fps": 30,
        "width": 320,
        "height": 240,
        "jpeg_quality": 60,
        "latency_ms": 10.0,
        "loss_percent": 5.0,
        "jitter_ms": 2.0,
    }
    # 1. Start
    res_start = client.post("/api/live/start", json=payload)
    assert res_start.status_code == 200
    assert res_start.json()["protocol"] == "UDP"

    time.sleep(0.6)

    # 2. Status
    res_status = client.get("/api/live/status")
    assert res_status.status_code == 200
    telemetry = res_status.json()
    assert telemetry["protocol"] == "UDP"
    assert telemetry["condition"]["loss_percent"] == 5.0

    # 3. Stop
    res_stop = client.post("/api/live/stop")
    assert res_stop.status_code == 200


def test_live_custom_video_streaming():
    """Test live streaming session with sample.mp4 video file."""
    client = TestClient(app)
    payload = {
        "protocol": "TCP",
        "video_source_type": "custom",
        "video_path": "sample.mp4",
        "total_frames": 12,
        "fps": 30,
        "width": 320,
        "height": 240,
        "jpeg_quality": 60,
    }
    res_start = client.post("/api/live/start", json=payload)
    assert res_start.status_code == 200
    data = res_start.json()
    assert data["status"] == "started"
    assert data["source"] == "custom"

    time.sleep(0.6)

    res_status = client.get("/api/live/status")
    assert res_status.status_code == 200
    assert res_status.json()["video_source"]["type"] == "custom"

    res_stop = client.post("/api/live/stop")
    assert res_stop.status_code == 200


def test_live_sse_telemetry():
    """Verify SSE telemetry stream endpoint delivers structured event-stream data."""
    client = TestClient(app)
    with client.stream("GET", "/api/live/telemetry?max_events=2") as res:
        assert res.status_code == 200
        assert "text/event-stream" in res.headers.get("content-type", "")
        lines = list(res.iter_lines())
        data_lines = [l for l in lines if l.startswith("data:")]
        assert len(data_lines) >= 1
        # Parse payload
        event_json = json.loads(data_lines[0].replace("data:", "").strip())
        assert "session_state" in event_json


def test_index_template_rendering_and_components():
    """Verify that the frontend HTML serves all required Mini-YouTube and Lab components."""
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    html = res.text

    # Core branding and structural elements
    assert "StreamNet Media Hub & Evaluation Lab" in html
    assert "catalogGrid" in html
    assert "watchPanel" in html
    assert "html5Player" in html
    assert "Baseline HTTP Media Stream" in html

    # Live experiment components
    assert "sectionExperiment" in html
    assert "liveFeedImg" in html
    assert "Audio is not transmitted" in html
    assert "Local Display Bridge" in html

    # API bindings
    assert "/api/media/catalog" in html
    assert "/api/live/start" in html
    assert "/api/live/stop" in html
    assert "/api/live/telemetry" in html
    assert "/api/live/video-feed" in html


def test_lan_compatibility_relative_api_urls():
    """Ensure frontend uses relative API URLs so it functions correctly across the LAN."""
    client = TestClient(app)
    res = client.get("/")
    html = res.text

    # Must NOT have hardcoded localhost/127.0.0.1 in fetch calls
    assert "fetch('http://localhost" not in html
    assert 'fetch("http://localhost' not in html
    assert "fetch('http://127.0.0.1" not in html
    assert 'fetch("http://127.0.0.1' not in html
    assert "EventSource('http://localhost" not in html
    assert "EventSource('http://127.0.0.1" not in html
