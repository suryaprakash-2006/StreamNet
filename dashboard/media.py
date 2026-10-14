"""
StreamNet Media Library & HTTP Media-Serving Subsystem.

Provides:
- Configurable host media directory for video files (STREAMNET_MEDIA_DIR)
- Recursive subfolder scanning for multi-video discovery
- Secure video catalog generation (stable ID, metadata, duration, codec tags, real thumbnails)
- Bulletproof path traversal prevention and ID resolution
- RFC 7233 compliant HTTP byte-range request serving for HTML5 video playback
- Full HEAD method support for browser preflight and player compatibility
- Media diagnostics helper reporting scanned, discovered, and ignored files
"""

from dataclasses import dataclass
import hashlib
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import socket
from typing import Any, Dict, Generator, List, Optional, Tuple

import cv2
from fastapi import HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse

logger = logging.getLogger("StreamNet.MediaLibrary")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MEDIA_DIR = BASE_DIR / "media"
SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wav"}

MIME_MAP = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wav": "audio/wav",
}


def get_local_ip_addresses() -> List[str]:
    """Retrieve non-loopback IPv4 addresses of this host for LAN access instructions."""
    ips = set()
    try:
        # UDP connect trick to find outgoing default interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
        finally:
            s.close()
    except Exception:
        pass

    try:
        # Fallback to gethostbyname_ex
        hostname = socket.gethostname()
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        for ip in ip_list:
            if not ip.startswith("127.") and ":" not in ip:
                ips.add(ip)
    except Exception:
        pass

    return sorted(list(ips)) or ["127.0.0.1"]


def get_media_dir() -> Path:
    """Return the configured media directory path."""
    env_dir = os.environ.get("STREAMNET_MEDIA_DIR")
    if env_dir:
        path = Path(env_dir).resolve()
    else:
        path = DEFAULT_MEDIA_DIR.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_thumbnails_dir(media_dir: Optional[Path] = None) -> Path:
    """Return the thumbnail cache directory."""
    base = (media_dir or get_media_dir()).resolve()
    thumb_dir = base / ".thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    return thumb_dir


def initialize_default_media(media_dir: Optional[Path] = None) -> None:
    """
    Ensure the media directory exists and populate sample.mp4 from project root if available.
    """
    target_dir = (media_dir or get_media_dir()).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    get_thumbnails_dir(target_dir)

    sample_root = BASE_DIR / "sample.mp4"
    sample_target = target_dir / "sample.mp4"
    if sample_root.exists() and not sample_target.exists():
        try:
            shutil.copy2(sample_root, sample_target)
            logger.info("Copied root sample.mp4 to media directory: %s", sample_target)
        except Exception as e:
            logger.warning("Failed to copy sample.mp4 to media directory: %s", e)

    sample_wav = target_dir / "sample.wav"
    if not sample_wav.exists():
        try:
            from server.audio_source import AudioSource
            AudioSource.create_sample_wav(str(sample_wav), duration_s=4.0, freq=440.0)
            logger.info("Created default sample.wav in media directory: %s", sample_wav)
        except Exception as e:
            logger.warning("Failed to create default sample.wav: %s", e)


def compute_video_id(relative_filename: str) -> str:
    """Generate a stable 16-hex-character video ID from relative filename."""
    norm = relative_filename.replace("\\", "/").strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def is_safe_video_id(video_id: str) -> bool:
    """Validate that video ID is strictly 16 hexadecimal characters."""
    return bool(re.match(r"^[a-f0-9]{16}$", video_id))


def extract_video_metadata(video_path: Path) -> Dict[str, Any]:
    """Extract resolution, fps, frame count, duration, and codec compatibility using OpenCV."""
    meta: Dict[str, Any] = {
        "width": None,
        "height": None,
        "fps": None,
        "frame_count": None,
        "duration_seconds": None,
        "codec": None,
        "is_browser_compatible": True,
        "codec_notice": None,
    }
    suffix = video_path.suffix.lower()
    if suffix == ".wav":
        try:
            import wave
            with wave.open(str(video_path), "rb") as wf:
                ch = wf.getnchannels()
                sr = wf.getframerate()
                nframes = wf.getnframes()
                meta["duration_seconds"] = round(nframes / float(sr), 2) if sr > 0 else 0.0
                meta["codec"] = f"PCM {ch}ch {sr}Hz"
                meta["is_browser_compatible"] = True
                meta["codec_notice"] = None
        except Exception as e:
            logger.debug("Failed to read WAV metadata for %s: %s", video_path, e)
        return meta

    try:
        cap = cv2.VideoCapture(str(video_path))
        if cap.isOpened():
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS))
            fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
            codec_tag = ""
            if fourcc_int > 0:
                try:
                    chars = [chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4)]
                    raw_tag = "".join(chars)
                    if all(c.isprintable() for c in raw_tag):
                        codec_tag = raw_tag.strip()
                except Exception:
                    pass

            cap.release()

            if w > 0 and h > 0:
                meta["width"] = w
                meta["height"] = h
            if fps > 0:
                meta["fps"] = round(fps, 2)
            if fc > 0:
                meta["frame_count"] = fc
            if fps > 0 and fc > 0:
                meta["duration_seconds"] = round(fc / fps, 2)

            if codec_tag:
                meta["codec"] = codec_tag

            # Determine browser compatibility based on container and codec
            tag_lower = codec_tag.lower() if codec_tag else ""

            if suffix == ".webm":
                meta["is_browser_compatible"] = True
            elif suffix == ".mp4":
                if any(k in tag_lower for k in ("avc1", "h264", "x264", "av01")):
                    meta["is_browser_compatible"] = True
                elif any(k in tag_lower for k in ("mp4v", "fmp4", "xvid", "divx")):
                    meta["is_browser_compatible"] = False
                    meta["codec_notice"] = (
                        f"Video encoded with '{codec_tag}' (MPEG-4 Part 2). Modern browsers require "
                        "H.264 (avc1) or WebM (vp8/vp9) for native HTML5 video playback."
                    )
                else:
                    # Default MP4 assuming modern H.264 if unspecified
                    meta["is_browser_compatible"] = True
            elif suffix in (".mkv", ".avi", ".mov"):
                if suffix == ".mov" and any(k in tag_lower for k in ("avc1", "h264")):
                    meta["is_browser_compatible"] = True
                else:
                    meta["is_browser_compatible"] = False
                    meta["codec_notice"] = (
                        f"Container format '{suffix}' is generally not supported natively by HTML5 browser "
                        "video players. Transcode to MP4 (H.264) or WebM for browser playback."
                    )
    except Exception as e:
        logger.debug("Failed to extract OpenCV metadata for %s: %s", video_path, e)
    return meta


def generate_thumbnail(video_path: Path, video_id: str, media_dir: Optional[Path] = None) -> Optional[Path]:
    """Generate or retrieve cached 320x180 JPEG thumbnail for a video."""
    thumb_dir = get_thumbnails_dir(media_dir)
    thumb_path = thumb_dir / f"{video_id}.jpg"

    # Return cached thumbnail if newer than video and non-empty
    if thumb_path.exists():
        try:
            if thumb_path.stat().st_mtime >= video_path.stat().st_mtime and thumb_path.stat().st_size > 0:
                return thumb_path
        except Exception:
            pass

    if video_path.suffix.lower() == ".wav":
        try:
            import numpy as np
            import math
            thumb = np.zeros((180, 320, 3), dtype=np.uint8)
            thumb[:] = (35, 28, 22)
            for x in range(20, 300, 10):
                bar_h = int(15 + 35 * abs(math.sin(x * 0.12)))
                cv2.rectangle(thumb, (x, 85 - bar_h), (x + 6, 85 + bar_h), (255, 180, 50), -1)
            cv2.putText(thumb, "AUDIO TRACK", (90, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 2)
            cv2.imwrite(str(thumb_path), thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return thumb_path if thumb_path.exists() else None
        except Exception:
            return None

    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        seek_frame = min(max(1, total_frames // 4), 30) if total_frames > 0 else 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, seek_frame)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None or frame.size == 0:
            return None

        # Resize to standard thumbnail 320x180
        thumb = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(thumb_path), thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return thumb_path if thumb_path.exists() else None
    except Exception as e:
        logger.warning("Error generating thumbnail for %s: %s", video_path, e)
        return None


def find_all_media_files(media_dir: Optional[Path] = None) -> List[Tuple[Path, str]]:
    """
    Recursively scan media directory for all supported video files.
    Returns list of (absolute_Path, relative_posix_path).
    Ignores hidden directories/files (starting with '.') and non-video files.
    """
    m_dir = (media_dir or get_media_dir()).resolve()
    if not m_dir.exists() or not m_dir.is_dir():
        return []

    results: List[Tuple[Path, str]] = []
    for root, dirs, files in os.walk(m_dir):
        # Prune hidden directories in-place (e.g. .thumbnails, .git)
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        root_path = Path(root)
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            fpath = root_path / fname
            if fpath.suffix.lower() in SUPPORTED_EXTENSIONS and fpath.is_file() and fpath.stat().st_size > 0:
                try:
                    rel_path = fpath.relative_to(m_dir).as_posix()
                    results.append((fpath, rel_path))
                except ValueError:
                    pass

    return sorted(results, key=lambda x: x[1])


def get_media_catalog(media_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Scan the media directory recursively and return catalog entries for all supported video files."""
    m_dir = (media_dir or get_media_dir()).resolve()
    if not m_dir.exists():
        return []

    entries: List[Dict[str, Any]] = []
    for fpath, rel_str in find_all_media_files(m_dir):
        video_id = compute_video_id(rel_str)
        file_size = fpath.stat().st_size
        meta = extract_video_metadata(fpath)
        thumb_path = generate_thumbnail(fpath, video_id, m_dir)
        has_thumb = thumb_path is not None and thumb_path.exists()

        # Format title cleanly, including parent folder indicator if nested
        clean_stem = fpath.stem.replace("_", " ").replace("-", " ").title()
        if "/" in rel_str:
            parent_folder = Path(rel_str).parent.as_posix().replace("_", " ").title()
            display_title = f"{clean_stem} ({parent_folder})"
        else:
            display_title = clean_stem

        item = {
            "id": video_id,
            "title": display_title,
            "filename": fpath.name,
            "relative_path": rel_str,
            "file_size_bytes": file_size,
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
        entries.append(item)

    return entries


def resolve_video_path(video_id: str, media_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Resolve a video ID to its underlying Path within the media directory.
    Enforces strict path traversal prevention:
      - Validates video_id format (16 hex chars)
      - Matches video_id against scanned safe files
      - Validates resolved path is strictly within media_dir
      - Validates file exists and is a regular file
    Returns Path or None.
    """
    if not is_safe_video_id(video_id):
        logger.warning("Rejected unsafe or malformed video ID: %r", video_id)
        return None

    m_dir = (media_dir or get_media_dir()).resolve()
    if not m_dir.exists():
        return None

    for fpath, rel_str in find_all_media_files(m_dir):
        if compute_video_id(rel_str) == video_id:
            resolved = fpath.resolve()
            try:
                if resolved.is_relative_to(m_dir) and resolved.is_file():
                    return resolved
            except AttributeError:
                if str(resolved).startswith(str(m_dir)) and resolved.is_file():
                    return resolved
    return None


def get_media_diagnostics(media_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Produce a comprehensive diagnostic report of the media library.
    Reports:
      - Configured media directory path and existence
      - Total files scanned across all subdirectories
      - Discovered valid video files with metadata & codec compatibility
      - Ignored files with specific reason
      - Host LAN IP addresses for multi-device access
    """
    m_dir = (media_dir or get_media_dir()).resolve()
    exists = m_dir.exists() and m_dir.is_dir()

    total_scanned = 0
    discovered = []
    ignored = []

    if exists:
        for root, dirs, files in os.walk(m_dir):
            skipped_dirs = [d for d in dirs if d.startswith(".")]
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for sd in skipped_dirs:
                ignored.append({
                    "path": (Path(root) / sd).relative_to(m_dir).as_posix(),
                    "reason": "hidden_directory_ignored",
                })

            root_path = Path(root)
            for fname in sorted(files):
                total_scanned += 1
                fpath = root_path / fname
                rel_path = fpath.relative_to(m_dir).as_posix()

                if fname.startswith("."):
                    ignored.append({"path": rel_path, "reason": "hidden_file_ignored"})
                    continue

                suffix = fpath.suffix.lower()
                if suffix not in SUPPORTED_EXTENSIONS:
                    ignored.append({
                        "path": rel_path,
                        "reason": f"unsupported_extension_{suffix or 'none'}",
                    })
                    continue

                if fpath.stat().st_size == 0:
                    ignored.append({"path": rel_path, "reason": "empty_file_zero_bytes"})
                    continue

                video_id = compute_video_id(rel_path)
                meta = extract_video_metadata(fpath)
                discovered.append({
                    "id": video_id,
                    "relative_path": rel_path,
                    "filename": fpath.name,
                    "file_size_bytes": fpath.stat().st_size,
                    "width": meta["width"],
                    "height": meta["height"],
                    "fps": meta["fps"],
                    "duration_seconds": meta["duration_seconds"],
                    "codec": meta["codec"],
                    "is_browser_compatible": meta["is_browser_compatible"],
                    "codec_notice": meta["codec_notice"],
                })

    host_ips = get_local_ip_addresses()

    return {
        "configured_media_dir": str(m_dir),
        "media_dir_exists": exists,
        "env_variable_set": "STREAMNET_MEDIA_DIR" in os.environ,
        "env_variable_value": os.environ.get("STREAMNET_MEDIA_DIR"),
        "total_files_scanned": total_scanned,
        "discovered_videos_count": len(discovered),
        "discovered_videos": discovered,
        "ignored_files_count": len(ignored),
        "ignored_files": ignored,
        "host_lan_ips": host_ips,
        "lan_access_urls": [f"http://{ip}:8000" for ip in host_ips],
        "supported_extensions": sorted(list(SUPPORTED_EXTENSIONS)),
        "troubleshooting_notes": [
            "If video appears black or fails to play in browser, verify codec is H.264 (avc1) or WebM (vp8/vp9). MPEG-4 Part 2 (mp4v/FMP4) is not supported natively by modern browsers.",
            "To make all files visible from another folder, start StreamNet with STREAMNET_MEDIA_DIR set to that folder.",
            "For LAN access, ensure Windows Firewall allows inbound connections on port 8000 or allow python.exe through the firewall.",
        ],
    }


def parse_byte_range(range_header: str, file_size: int) -> Tuple[int, int]:
    """
    Parse RFC 7233 byte-range header: 'bytes=start-end'.
    Returns (start, end) where end is inclusive.
    Raises ValueError if range format is invalid or unsatisfiable.
    """
    if not range_header or not range_header.startswith("bytes="):
        raise ValueError("Invalid range header format")

    range_spec = range_header[6:].strip()
    if "," in range_spec:
        range_spec = range_spec.split(",")[0].strip()

    parts = range_spec.split("-")
    if len(parts) != 2:
        raise ValueError("Invalid range specification")

    start_str, end_str = parts[0].strip(), parts[1].strip()
    if not start_str and not end_str:
        raise ValueError("Empty range")

    if not start_str:
        suffix_len = int(end_str)
        if suffix_len <= 0:
            raise ValueError("Invalid suffix range length")
        start = max(0, file_size - suffix_len)
        end = file_size - 1
    elif not end_str:
        start = int(start_str)
        end = file_size - 1
    else:
        start = int(start_str)
        end = int(end_str)

    if start < 0 or start >= file_size or end < start:
        raise ValueError(f"Range not satisfiable: {start}-{end} for size {file_size}")

    end = min(end, file_size - 1)
    return start, end


def ranged_file_iterator(file_path: Path, start: int, end: int, chunk_size: int = 64 * 1024) -> Generator[bytes, None, None]:
    """Stream binary byte chunks from start to end inclusive."""
    with open(file_path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            read_len = min(remaining, chunk_size)
            chunk = f.read(read_len)
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def create_media_stream_response(
    video_path: Path,
    range_header: Optional[str] = None,
    request_method: str = "GET",
) -> Response:
    """
    Serve video file with full RFC 7233 HTTP byte-range and HEAD method support.
    Returns 206 Partial Content, 200 OK, or 416 Range Not Satisfiable.
    """
    file_size = video_path.stat().st_size
    mime_type = MIME_MAP.get(video_path.suffix.lower(), "video/mp4")
    is_head = request_method.upper() == "HEAD"

    if not range_header:
        if is_head:
            return Response(
                status_code=200,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(file_size),
                    "Content-Type": mime_type,
                    "Cache-Control": "public, max-age=3600",
                },
            )
        return StreamingResponse(
            ranged_file_iterator(video_path, 0, file_size - 1),
            status_code=200,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Content-Type": mime_type,
                "Cache-Control": "public, max-age=3600",
            },
        )

    try:
        start, end = parse_byte_range(range_header, file_size)
    except ValueError as err:
        logger.info("Range not satisfiable for %s: %s", video_path.name, err)
        return Response(
            status_code=416,
            headers={
                "Content-Range": f"bytes */{file_size}",
                "Accept-Ranges": "bytes",
            },
            content="" if is_head else "Requested Range Not Satisfiable",
        )

    content_length = end - start + 1
    if is_head:
        return Response(
            status_code=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
                "Content-Type": mime_type,
                "Cache-Control": "public, max-age=3600",
            },
        )

    return StreamingResponse(
        ranged_file_iterator(video_path, start, end),
        status_code=206,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": mime_type,
            "Cache-Control": "public, max-age=3600",
        },
    )
