"""
Tests for StreamNet Host Media Library, Video Catalogue, and RFC 7233 HTTP Byte-Range Streaming.
"""

import os
from pathlib import Path
import shutil
import tempfile
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.media import compute_video_id, get_media_dir, initialize_default_media
from dashboard.session import session_manager


@pytest.fixture
def temp_media_dir(monkeypatch):
    """Create an isolated temporary media directory for testing."""
    tmp = tempfile.mkdtemp(prefix="streamnet_media_test_")
    monkeypatch.setenv("STREAMNET_MEDIA_DIR", tmp)
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def create_tiny_test_video(output_path: Path, num_frames: int = 15, width: int = 160, height: int = 120, fps: int = 30):
    """Generate a valid, compact MP4 test video with colored frames."""
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
    for i in range(num_frames):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        color = ((i * 15) % 255, (i * 30) % 255, 200)
        cv2.circle(frame, (width // 2, height // 2), 20 + i, color, -1)
        writer.write(frame)
    writer.release()


def test_empty_media_directory(temp_media_dir):
    """Verify that an empty media directory yields an empty catalog without error."""
    client = TestClient(app)
    res = client.get("/api/media/catalog")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 0
    assert data["videos"] == []


def test_catalog_listing_and_metadata(temp_media_dir):
    """Verify catalogue returns correct stable ID, metadata, duration, and thumbnail status."""
    video_file = temp_media_dir / "clip_alpha.mp4"
    create_tiny_test_video(video_file, num_frames=30, width=160, height=120, fps=30)

    client = TestClient(app)
    res = client.get("/api/media/catalog")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1

    item = data["videos"][0]
    expected_id = compute_video_id("clip_alpha.mp4")
    assert item["id"] == expected_id
    assert item["title"] == "Clip Alpha"
    assert item["filename"] == "clip_alpha.mp4"
    assert item["file_size_bytes"] == video_file.stat().st_size
    assert item["duration_seconds"] == 1.0  # 30 frames at 30 fps
    assert item["width"] == 160
    assert item["height"] == 120
    assert item["fps"] == 30.0
    assert item["has_thumbnail"] is True
    assert item["thumbnail_url"] == f"/api/media/{expected_id}/thumbnail"
    assert item["stream_url"] == f"/api/media/{expected_id}/stream"


def test_get_video_item_by_id(temp_media_dir):
    """Verify retrieving a single video item by ID."""
    video_file = temp_media_dir / "test_item.mp4"
    create_tiny_test_video(video_file, num_frames=15, width=160, height=120, fps=30)
    video_id = compute_video_id("test_item.mp4")

    client = TestClient(app)
    res = client.get(f"/api/media/{video_id}")
    assert res.status_code == 200
    item = res.json()
    assert item["id"] == video_id
    assert item["filename"] == "test_item.mp4"


def test_unknown_video_id(temp_media_dir):
    """Verify 404 responses for non-existent video IDs."""
    client = TestClient(app)
    unknown_id = "0123456789abcdef"

    res_item = client.get(f"/api/media/{unknown_id}")
    assert res_item.status_code == 404

    res_stream = client.get(f"/api/media/{unknown_id}/stream")
    assert res_stream.status_code == 404

    res_thumb = client.get(f"/api/media/{unknown_id}/thumbnail")
    assert res_thumb.status_code == 404


def test_path_traversal_prevention(temp_media_dir):
    """Verify strict rejection of path traversal attempts and unsafe IDs."""
    client = TestClient(app)
    unsafe_payloads = [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "..\\..\\windows\\system32",
        "../../sample.mp4",
        "../test",
        "/etc/shadow",
        "C:\\windows\\system32",
        "invalid-non-hex-id-value",
        "12345",  # Too short
        "0123456789abcdef0123456789abcdef",  # Too long
    ]

    for bad_id in unsafe_payloads:
        res_get = client.get(f"/api/media/{bad_id}")
        assert res_get.status_code in (400, 404), f"Payload {bad_id} was not rejected"

        res_stream = client.get(f"/api/media/{bad_id}/stream")
        assert res_stream.status_code in (400, 404), f"Stream payload {bad_id} was not rejected"

        res_thumb = client.get(f"/api/media/{bad_id}/thumbnail")
        assert res_thumb.status_code in (400, 404), f"Thumbnail payload {bad_id} was not rejected"


def test_byte_range_requests_seekable(temp_media_dir):
    """Verify RFC 7233 byte-range request handling for browser media seeking."""
    video_file = temp_media_dir / "range_test.mp4"
    create_tiny_test_video(video_file, num_frames=20, width=160, height=120, fps=30)
    video_id = compute_video_id("range_test.mp4")
    file_bytes = video_file.read_bytes()
    file_size = len(file_bytes)

    client = TestClient(app)

    # 1. Full file request (no Range header) -> 200 OK
    res_full = client.get(f"/api/media/{video_id}/stream")
    assert res_full.status_code == 200
    assert res_full.headers["Accept-Ranges"] == "bytes"
    assert int(res_full.headers["Content-Length"]) == file_size
    assert res_full.content == file_bytes

    # 2. First 100 bytes: bytes=0-99 -> 206 Partial Content
    res_start = client.get(f"/api/media/{video_id}/stream", headers={"Range": "bytes=0-99"})
    assert res_start.status_code == 206
    assert res_start.headers["Content-Range"] == f"bytes 0-99/{file_size}"
    assert int(res_start.headers["Content-Length"]) == 100
    assert res_start.content == file_bytes[0:100]

    # 3. Middle range: bytes=150-249 -> 206 Partial Content
    res_mid = client.get(f"/api/media/{video_id}/stream", headers={"Range": "bytes=150-249"})
    assert res_mid.status_code == 206
    assert res_mid.headers["Content-Range"] == f"bytes 150-249/{file_size}"
    assert int(res_mid.headers["Content-Length"]) == 100
    assert res_mid.content == file_bytes[150:250]

    # 4. Open-ended range: bytes=200- -> 206 Partial Content
    res_tail = client.get(f"/api/media/{video_id}/stream", headers={"Range": "bytes=200-"})
    assert res_tail.status_code == 206
    assert res_tail.headers["Content-Range"] == f"bytes 200-{file_size - 1}/{file_size}"
    assert int(res_tail.headers["Content-Length"]) == file_size - 200
    assert res_tail.content == file_bytes[200:]

    # 5. Suffix range: bytes=-64 -> 206 Partial Content
    res_suffix = client.get(f"/api/media/{video_id}/stream", headers={"Range": "bytes=-64"})
    assert res_suffix.status_code == 206
    assert res_suffix.headers["Content-Range"] == f"bytes {file_size - 64}-{file_size - 1}/{file_size}"
    assert int(res_suffix.headers["Content-Length"]) == 64
    assert res_suffix.content == file_bytes[-64:]

    # 6. Unsatisfiable range: bytes=99999999- -> 416 Range Not Satisfiable
    res_unsat = client.get(f"/api/media/{video_id}/stream", headers={"Range": "bytes=99999999-"})
    assert res_unsat.status_code == 416
    assert res_unsat.headers["Content-Range"] == f"bytes */{file_size}"


def test_thumbnail_generation_and_retrieval(temp_media_dir):
    """Verify real thumbnail generation and retrieval."""
    video_file = temp_media_dir / "thumb_test.mp4"
    create_tiny_test_video(video_file, num_frames=15, width=160, height=120, fps=30)
    video_id = compute_video_id("thumb_test.mp4")

    client = TestClient(app)
    res_thumb = client.get(f"/api/media/{video_id}/thumbnail")
    assert res_thumb.status_code == 200
    assert "image/jpeg" in res_thumb.headers["Content-Type"]
    assert len(res_thumb.content) > 0

    # Verify cached thumbnail file exists
    cached_thumb = temp_media_dir / ".thumbnails" / f"{video_id}.jpg"
    assert cached_thumb.exists()
    assert cached_thumb.stat().st_size > 0


def test_corrupted_or_invalid_video_file_in_catalog(temp_media_dir):
    """Verify that a corrupted or non-video file with .mp4 extension does not crash catalogue."""
    corrupted_file = temp_media_dir / "corrupted_file.mp4"
    corrupted_file.write_bytes(b"This is not a valid video file at all.")
    corrupted_id = compute_video_id("corrupted_file.mp4")

    client = TestClient(app)
    res = client.get("/api/media/catalog")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1

    item = data["videos"][0]
    assert item["id"] == corrupted_id
    assert item["duration_seconds"] is None
    assert item["has_thumbnail"] is False

    # Thumbnail endpoint returns 404 for corrupted video
    res_thumb = client.get(f"/api/media/{corrupted_id}/thumbnail")
    assert res_thumb.status_code == 404


def test_live_start_using_video_id(temp_media_dir):
    """Verify starting a live streaming session using a video ID from media library."""
    session_manager.stop_session()
    video_file = temp_media_dir / "live_sample.mp4"
    create_tiny_test_video(video_file, num_frames=10, width=160, height=120, fps=30)
    video_id = compute_video_id("live_sample.mp4")

    client = TestClient(app)
    payload = {
        "protocol": "TCP",
        "video_source_type": "custom",
        "video_path": video_id,  # Passing video ID instead of raw filesystem path
        "total_frames": 10,
        "fps": 30,
        "width": 160,
        "height": 120,
    }
    res = client.post("/api/live/start", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] == "started"

    session_manager.stop_session()


def test_multi_video_discovery_and_nested_directories(temp_media_dir):
    """Verify multi-video discovery, subdirectory scanning, and correct ID calculation."""
    # Top-level video
    v1 = temp_media_dir / "alpha.mp4"
    create_tiny_test_video(v1, num_frames=10, width=160, height=120, fps=30)

    # Subdirectory video
    sub_dir = temp_media_dir / "season_1"
    sub_dir.mkdir(parents=True, exist_ok=True)
    v2 = sub_dir / "episode_1.mp4"
    create_tiny_test_video(v2, num_frames=15, width=160, height=120, fps=30)

    # Deeply nested video
    nested_dir = temp_media_dir / "archive" / "2026"
    nested_dir.mkdir(parents=True, exist_ok=True)
    v3 = nested_dir / "clip_archived.mp4"
    create_tiny_test_video(v3, num_frames=20, width=160, height=120, fps=30)

    # Non-video and empty files that should be ignored
    (temp_media_dir / "readme.txt").write_text("Not a video")
    (temp_media_dir / "empty.mp4").touch()

    client = TestClient(app)
    res = client.get("/api/media/catalog")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 3

    discovered_ids = {item["id"] for item in data["videos"]}
    expected_id_1 = compute_video_id("alpha.mp4")
    expected_id_2 = compute_video_id("season_1/episode_1.mp4")
    expected_id_3 = compute_video_id("archive/2026/clip_archived.mp4")

    assert expected_id_1 in discovered_ids
    assert expected_id_2 in discovered_ids
    assert expected_id_3 in discovered_ids

    # Verify nested item title and relative path
    item_nested = next(item for item in data["videos"] if item["id"] == expected_id_2)
    assert "Episode 1" in item_nested["title"]
    assert "Season 1" in item_nested["title"]
    assert item_nested["relative_path"] == "season_1/episode_1.mp4"

    # Verify retrieval and streaming of nested item
    res_get = client.get(f"/api/media/{expected_id_2}")
    assert res_get.status_code == 200
    assert res_get.json()["filename"] == "episode_1.mp4"

    res_stream = client.get(f"/api/media/{expected_id_2}/stream")
    assert res_stream.status_code == 200
    assert len(res_stream.content) == v2.stat().st_size


def test_head_requests_for_stream_and_thumbnail(temp_media_dir):
    """Verify HTTP HEAD method support for browser video players and preflight checks."""
    v_file = temp_media_dir / "head_test.mp4"
    create_tiny_test_video(v_file, num_frames=20, width=160, height=120, fps=30)
    v_id = compute_video_id("head_test.mp4")
    file_size = v_file.stat().st_size

    client = TestClient(app)

    # 1. Full HEAD request on stream endpoint
    res_head = client.head(f"/api/media/{v_id}/stream")
    assert res_head.status_code == 200
    assert res_head.headers["Accept-Ranges"] == "bytes"
    assert int(res_head.headers["Content-Length"]) == file_size
    assert "video/mp4" in res_head.headers["Content-Type"]
    assert len(res_head.content) == 0  # HEAD must not return a body

    # 2. Ranged HEAD request on stream endpoint
    res_head_range = client.head(f"/api/media/{v_id}/stream", headers={"Range": "bytes=0-49"})
    assert res_head_range.status_code == 206
    assert res_head_range.headers["Content-Range"] == f"bytes 0-49/{file_size}"
    assert int(res_head_range.headers["Content-Length"]) == 50
    assert len(res_head_range.content) == 0

    # 3. HEAD request on thumbnail endpoint
    # First generate thumbnail via GET or direct call
    client.get(f"/api/media/{v_id}/thumbnail")
    res_thumb_head = client.head(f"/api/media/{v_id}/thumbnail")
    assert res_thumb_head.status_code == 200
    assert "image/jpeg" in res_thumb_head.headers["Content-Type"]
    assert int(res_thumb_head.headers["Content-Length"]) > 0
    assert len(res_thumb_head.content) == 0


def test_diagnostic_endpoint(temp_media_dir):
    """Verify GET /api/media/diagnostic reports directory, scanned, discovered, and ignored files."""
    v1 = temp_media_dir / "diag_clip.mp4"
    create_tiny_test_video(v1, num_frames=10, width=160, height=120, fps=30)
    (temp_media_dir / "ignored_file.txt").write_text("Ignored text file")
    (temp_media_dir / "zero_byte.mp4").touch()

    client = TestClient(app)
    res = client.get("/api/media/diagnostic")
    assert res.status_code == 200
    data = res.json()

    assert data["media_dir_exists"] is True
    assert data["configured_media_dir"] == str(temp_media_dir)
    assert data["discovered_videos_count"] >= 1
    assert data["ignored_files_count"] >= 2

    # Check discovered video details
    found = next(v for v in data["discovered_videos"] if v["filename"] == "diag_clip.mp4")
    assert "codec" in found
    assert "is_browser_compatible" in found

    # Check ignored reasons
    reasons = [ig["reason"] for ig in data["ignored_files"]]
    assert any("unsupported_extension" in r for r in reasons)
    assert any("empty_file_zero_bytes" in r for r in reasons)

    # Check LAN IPs
    assert len(data["host_lan_ips"]) > 0
    assert len(data["lan_access_urls"]) > 0
    assert len(data["troubleshooting_notes"]) >= 3


def test_direct_media_route_access(temp_media_dir):
    """Verify /media/{path} and /sample.mp4 convenience endpoints."""
    v_file = temp_media_dir / "direct.mp4"
    create_tiny_test_video(v_file, num_frames=15, width=160, height=120, fps=30)

    client = TestClient(app)

    # 1. Direct GET
    res_direct = client.get("/media/direct.mp4")
    assert res_direct.status_code == 200
    assert res_direct.headers["Accept-Ranges"] == "bytes"
    assert len(res_direct.content) == v_file.stat().st_size

    # 2. Direct HEAD
    res_head = client.head("/media/direct.mp4")
    assert res_head.status_code == 200
    assert len(res_head.content) == 0

    # 3. Path traversal rejection on direct endpoint
    res_trav = client.get("/media/../../secret.txt")
    assert res_trav.status_code in (403, 404)

