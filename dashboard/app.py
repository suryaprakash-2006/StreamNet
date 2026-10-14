"""
StreamNet Web Dashboard and Interactive Experiment Interface.

Provides a modern web-based graphical interface for:
  - Triggering live TCP vs UDP benchmarks with custom network degradation sliders
  - Inspecting master experiment telemetry and comparison charts
  - Visualizing publication-grade graphs
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dashboard.media import (
    SUPPORTED_EXTENSIONS,
    create_media_stream_response,
    extract_video_metadata,
    generate_thumbnail,
    get_media_catalog,
    get_media_diagnostics,
    get_media_dir,
    initialize_default_media,
    is_safe_video_id,
    resolve_video_path,
)
from dashboard.session import SessionConfig, SessionState, session_manager
from experiments.runner import ExperimentRunner
from experiments.scenarios import ExperimentScenario
from experiments.visualizer import generate_all_figures, load_master_dataframe
from network.conditions import NetworkCondition
from network.profiles import PROFILES

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = BASE_DIR / "experiments" / "results"
PLOTS_DIR = RESULTS_DIR / "plots"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

RESULTS_DIR.mkdir(parents=True, exist_ok=True)
PLOTS_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
initialize_default_media()

app = FastAPI(title="StreamNet Evaluation Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/plots", StaticFiles(directory=str(PLOTS_DIR)), name="plots")


class RunRequest(BaseModel):
    scenario_name: str = "Custom Live Run"
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    bandwidth_mbps: Optional[float] = None
    total_frames: int = 90
    fps: int = 30
    jpeg_quality: int = 70


class LiveStartRequest(BaseModel):
    protocol: str = "TCP"                  # "TCP" or "UDP"
    media_mode: str = "video"              # "video", "audio", or "audio_video"
    video_source_type: str = "synthetic"   # "synthetic" or "custom"
    video_path: Optional[str] = None       # e.g. "sample.mp4"
    audio_source_type: str = "synthetic"   # "synthetic", "custom", or "mic"
    audio_path: Optional[str] = None       # e.g. "sample.wav"
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_tone_freq: float = 440.0
    audio_pattern: str = "sine"            # "sine" or "ping"
    total_frames: int = 150                # Total frames to stream (0 for full file)
    fps: int = 30                          # Target playout rate
    width: int = 640                       # Video width
    height: int = 480                      # Video height
    jpeg_quality: int = 75                 # JPEG encoding quality
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    bandwidth_mbps: Optional[float] = None


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>StreamNet Dashboard - Template loading error</h1>", status_code=500)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/profiles")
async def get_profiles():
    """Return all available predefined network profiles."""
    return {
        key: {
            "name": cond.name,
            "latency_ms": cond.latency_ms,
            "jitter_ms": cond.jitter_ms,
            "loss_percent": cond.loss_percent,
            "bandwidth_mbps": cond.bandwidth_mbps,
        }
        for key, cond in PROFILES.items()
    }


@app.get("/api/experiments")
async def get_experiments():
    """Return all historical experiment records from master CSV."""
    csv_file = RESULTS_DIR / "master_experiment_results.csv"
    if not csv_file.exists():
        return []
    df = load_master_dataframe(csv_file)
    return df.to_dict(orient="records")


@app.post("/api/run-scenario")
async def run_live_scenario(req: RunRequest):
    """Execute a live paired TCP vs UDP experiment with the requested degradation parameters."""
    condition = NetworkCondition(
        name=req.scenario_name,
        latency_ms=req.latency_ms,
        jitter_ms=req.jitter_ms,
        loss_percent=req.loss_percent,
        bandwidth_mbps=req.bandwidth_mbps if (req.bandwidth_mbps and req.bandwidth_mbps > 0) else None,
    )

    scenario = ExperimentScenario(
        scenario_id=f"LIVE_{int(req.latency_ms)}D_{int(req.loss_percent)}L",
        name=req.scenario_name,
        category="live_dashboard",
        condition=condition,
        total_frames=req.total_frames,
        fps=req.fps,
        jpeg_quality=req.jpeg_quality,
    )

    runner = ExperimentRunner(results_dir=RESULTS_DIR)
    record = runner.run_scenario(scenario)

    # Regenerate publication figures in background
    try:
        generate_all_figures(output_dir=PLOTS_DIR)
    except Exception:
        pass

    return record


@app.post("/api/generate-plots")
async def generate_plots():
    """Trigger plot generation from master CSV."""
    csv_file = RESULTS_DIR / "master_experiment_results.csv"
    if not csv_file.exists():
        raise HTTPException(status_code=404, detail="No experiment data found.")
    plots = generate_all_figures(output_dir=PLOTS_DIR)
    return {"generated_plots": [p.name for p in plots]}


@app.get("/api/plots")
async def list_plots():
    """List available plot image filenames."""
    plot_files = list(PLOTS_DIR.glob("*.png"))
    return [p.name for p in sorted(plot_files)]


# ---------------------------------------------------------
# Live Real-Time Browser Video Streaming & Telemetry Endpoints
# ---------------------------------------------------------

@app.post("/api/live/start")
async def start_live_session(req: LiveStartRequest):
    """Start an active real-time TCP or UDP streaming session with controlled network impairment."""
    protocol = req.protocol.upper()
    if protocol not in ("TCP", "UDP"):
        raise HTTPException(status_code=400, detail="Protocol must be either 'TCP' or 'UDP'.")

    # Resolve video path if custom video source requested
    resolved_video_path = None
    if req.video_source_type == "custom":
        if not req.video_path:
            raise HTTPException(status_code=400, detail="video_path must be specified when video_source_type is 'custom'.")
        # Support resolving from media library video_id if provided
        resolved_from_id = resolve_video_path(req.video_path)
        if resolved_from_id is not None:
            resolved_video_path = str(resolved_from_id)
        else:
            target_path = Path(req.video_path)
            if not target_path.is_absolute():
                target_path = BASE_DIR / target_path
            if not target_path.exists():
                raise HTTPException(status_code=400, detail=f"Custom video file not found: {req.video_path}")
            resolved_video_path = str(target_path)

    # Resolve audio path if custom audio is requested
    resolved_audio_path = None
    if req.audio_source_type == "custom" and req.audio_path:
        if is_safe_video_id(req.audio_path):
            resolved_from_id = resolve_video_path(req.audio_path)
            if resolved_from_id and resolved_from_id.exists():
                resolved_audio_path = str(resolved_from_id)
        if not resolved_audio_path:
            target_a_path = Path(req.audio_path)
            if not target_a_path.is_absolute():
                target_a_path = BASE_DIR / target_a_path
            if not target_a_path.exists():
                raise HTTPException(status_code=400, detail=f"Custom audio file not found: {req.audio_path}")
            resolved_audio_path = str(target_a_path)

    cond = NetworkCondition(
        name=f"Live_{protocol}_{req.latency_ms}ms_{req.loss_percent}%",
        latency_ms=req.latency_ms,
        jitter_ms=req.jitter_ms,
        loss_percent=req.loss_percent,
        bandwidth_mbps=req.bandwidth_mbps if (req.bandwidth_mbps and req.bandwidth_mbps > 0) else None,
    )

    config = SessionConfig(
        protocol=protocol,
        media_mode=req.media_mode,
        video_source_type=req.video_source_type,
        video_path=resolved_video_path,
        audio_source_type=req.audio_source_type,
        audio_path=resolved_audio_path,
        audio_sample_rate=req.audio_sample_rate,
        audio_channels=req.audio_channels,
        audio_tone_freq=req.audio_tone_freq,
        audio_pattern=req.audio_pattern,
        total_frames=req.total_frames,
        fps=req.fps,
        width=req.width,
        height=req.height,
        jpeg_quality=req.jpeg_quality,
        condition=cond,
    )

    try:
        session = session_manager.start_session(config)
        return {
            "status": "started",
            "protocol": session.protocol,
            "session_state": session.state.value,
            "media_mode": req.media_mode,
            "source": req.video_source_type,
            "condition": {
                "latency_ms": cond.latency_ms,
                "loss_percent": cond.loss_percent,
                "bandwidth_mbps": cond.bandwidth_mbps,
                "jitter_ms": cond.jitter_ms,
            },
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Failed to start live session: {err}")


@app.post("/api/live/stop")
async def stop_live_session():
    """Stop any currently active live streaming session and release sockets."""
    session_manager.stop_session()
    return {"status": "stopped", "session_state": "STOPPED"}


@app.get("/api/live/status")
async def get_live_status():
    """Retrieve the current state and latest metrics of the active live streaming session."""
    session = session_manager.get_current_session()
    if session is None:
        return {"session_state": "IDLE"}
    return session.get_telemetry()


@app.get("/api/live/video-feed")
async def get_live_video_feed():
    """Streams client-received video frames as MJPEG multipart/x-mixed-replace content."""
    session = session_manager.get_current_session()
    if session is None or session.state not in (SessionState.STARTING, SessionState.STREAMING):
        raise HTTPException(status_code=404, detail="No active live streaming session")

    return StreamingResponse(
        session.generate_mjpeg_stream_async(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, private",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/live/audio-feed")
async def get_live_audio_feed():
    """Streams client-received audio frames as standard streaming WAV content."""
    session = session_manager.get_current_session()
    if session is None or session.state not in (SessionState.STARTING, SessionState.STREAMING):
        raise HTTPException(status_code=404, detail="No active live streaming session")

    return StreamingResponse(
        session.generate_audio_stream_async(),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-cache, private",
            "Pragma": "no-cache",
        },
    )


@app.post("/api/live/mic-upload")
async def upload_mic_audio(request: Request):
    """Receive live audio PCM samples from browser microphone."""
    session = session_manager.get_current_session()
    if session is None or session.state not in (SessionState.STARTING, SessionState.STREAMING):
        raise HTTPException(status_code=404, detail="No active live streaming session")

    pcm_data = await request.body()
    if pcm_data:
        session.push_mic_audio(pcm_data)
        return {"status": "ok", "bytes_received": len(pcm_data)}
    return {"status": "empty"}


@app.get("/api/live/telemetry")
async def get_live_telemetry_stream(request: Request, max_events: Optional[int] = None):
    """SSE endpoint streaming live telemetry updates at ~10 Hz."""
    async def sse_generator():
        events_sent = 0
        while True:
            if await request.is_disconnected():
                break
            session = session_manager.get_current_session()
            if session is not None:
                telemetry = session.get_telemetry()
                yield f"data: {json.dumps(telemetry)}\n\n"
                events_sent += 1
                if max_events is not None and events_sent >= max_events:
                    break
                if session.state in (SessionState.STOPPED, SessionState.ERROR):
                    await asyncio.sleep(0.2)
                    break
            else:
                yield f"data: {json.dumps({'session_state': 'IDLE'})}\n\n"
                events_sent += 1
                if max_events is not None and events_sent >= max_events:
                    break
            await asyncio.sleep(0.1)

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------
# Host Media Library & HTTP Streaming Endpoints (Mini-YouTube)
# ---------------------------------------------------------

@app.get("/api/media/catalog")
@app.get("/api/media/videos")
async def get_catalog():
    """Retrieve the video catalogue for the host media library."""
    catalog = get_media_catalog()
    return {
        "media_dir": str(get_media_dir()),
        "count": len(catalog),
        "videos": catalog,
    }


@app.get("/api/media/diagnostic")
async def get_media_diagnostic_info():
    """Retrieve full media library diagnostics, discovered files, ignored files, and LAN status."""
    return get_media_diagnostics()


@app.get("/api/media/{video_id}")
async def get_video_item(video_id: str):
    """Retrieve metadata for a specific video in the media library."""
    if not is_safe_video_id(video_id):
        raise HTTPException(status_code=400, detail="Invalid video ID format; path traversal not permitted.")

    video_path = resolve_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found in media library.")

    meta = extract_video_metadata(video_path)
    thumb_path = generate_thumbnail(video_path, video_id)
    has_thumb = thumb_path is not None and thumb_path.exists()
    clean_title = video_path.stem.replace("_", " ").replace("-", " ").title()

    return {
        "id": video_id,
        "title": clean_title,
        "filename": video_path.name,
        "file_size_bytes": video_path.stat().st_size,
        "duration_seconds": meta["duration_seconds"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "codec": meta["codec"],
        "is_browser_compatible": meta["is_browser_compatible"],
        "codec_notice": meta["codec_notice"],
        "has_thumbnail": has_thumb,
        "thumbnail_url": f"/api/media/{video_id}/thumbnail" if has_thumb else None,
        "stream_url": f"/api/media/{video_id}/stream",
    }


@app.api_route("/api/media/{video_id}/stream", methods=["GET", "HEAD"])
async def stream_media_file(video_id: str, request: Request):
    """
    Serve video content with RFC 7233 byte-range and HEAD support for seekable browser playback.
    Enforces strict path traversal prevention.
    """
    if not is_safe_video_id(video_id):
        raise HTTPException(status_code=400, detail="Invalid video ID format; path traversal not permitted.")

    video_path = resolve_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found in media library.")

    range_header = request.headers.get("range")
    return create_media_stream_response(video_path, range_header, request_method=request.method)


@app.api_route("/api/media/{video_id}/thumbnail", methods=["GET", "HEAD"])
async def get_video_thumbnail(video_id: str, request: Request):
    """Serve generated JPEG thumbnail for a video with HEAD support."""
    if not is_safe_video_id(video_id):
        raise HTTPException(status_code=400, detail="Invalid video ID format; path traversal not permitted.")

    video_path = resolve_video_path(video_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Video not found in media library.")

    thumb_path = generate_thumbnail(video_path, video_id)
    if not thumb_path or not thumb_path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not available for this video.")

    if request.method == "HEAD":
        return Response(
            status_code=200,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Length": str(thumb_path.stat().st_size),
                "Cache-Control": "public, max-age=86400",
            },
        )

    return FileResponse(
        thumb_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.api_route("/media/{file_path:path}", methods=["GET", "HEAD"])
async def stream_media_direct(file_path: str, request: Request):
    """
    Direct endpoint for accessing videos by relative file path within media directory.
    Provides backward-compatibility and prevents 404 on direct URL navigation.
    """
    m_dir = get_media_dir().resolve()
    target = (m_dir / file_path).resolve()
    try:
        if not target.is_relative_to(m_dir):
            raise HTTPException(status_code=403, detail="Access denied: path traversal not permitted.")
    except AttributeError:
        if not str(target).startswith(str(m_dir)):
            raise HTTPException(status_code=403, detail="Access denied: path traversal not permitted.")

    if not target.exists() or not target.is_file() or target.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=404, detail="Video file not found in media directory.")

    range_header = request.headers.get("range")
    return create_media_stream_response(target, range_header, request_method=request.method)





if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
