"""Service layer for VOD Studio: chunked uploads, project CRUD, scouting, and export."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import inspect
import json
import logging
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse
import uuid

import aiosqlite
from fastapi import HTTPException, status

from app.config import Settings
from app.db import open_connection, utcnow_iso
from app.services.neon_client import NeonClippingClient
from app.services.processing_queue import _extract_thumbnail_jpeg
from app.services.recorder import find_binary, probe_duration
from app.services.subprocess_compat import create_subprocess_exec
from app.storage import ensure_studio_dir, resolve_safe_path, slugify_streamer

logger = logging.getLogger(__name__)

UPLOAD_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm"})
CHAT_EXTENSIONS = frozenset({".json", ".jsonl", ".txt"})
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
_TWITCH_HOSTS = frozenset({"twitch.tv", "www.twitch.tv", "m.twitch.tv"})
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})
_YTDLP_PCT_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _kill_pid_tree(proc: Any) -> None:
    """Kill a subprocess and any grandchildren (e.g. yt-dlp -> ffmpeg) cleanly."""
    pid = getattr(proc, "pid", None)
    if pid and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _cleanup_download_partials(dest_mp4: Path) -> None:
    """Remove a target VOD download file and any yt-dlp .part / fragment siblings."""
    dest_mp4.unlink(missing_ok=True)
    if dest_mp4.parent.is_dir():
        for sibling in dest_mp4.parent.glob(f"{dest_mp4.name}*"):
            if sibling.is_file():
                sibling.unlink(missing_ok=True)


def parse_vod_url(raw_url: str) -> tuple[str, str]:
    """Validate and classify a Twitch or YouTube VOD URL."""
    url = str(raw_url or "").strip()
    if not url or len(url) > 1000 or any(ch.isspace() for ch in url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enter a valid Twitch or YouTube VOD URL",
        )
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="VOD URL must start with https:// or http://",
        )
    if host in _TWITCH_HOSTS:
        return "twitch", url
    if host in _YOUTUBE_HOSTS:
        return "youtube", url
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Only Twitch (twitch.tv) and YouTube (youtube.com / youtu.be) VOD URLs are supported",
    )


def parse_time_offset(raw: str | float | int | None, field_name: str = "time") -> float | None:
    """Parse HH:MM:SS, MM:SS, or seconds string into non-negative float seconds."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if not math.isfinite(val) or val < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid {field_name}: must be >= 0",
            )
        return val
    text = str(raw).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        if len(parts) == 1:
            val = float(parts[0])
        elif len(parts) == 2:
            val = int(parts[0]) * 60.0 + float(parts[1])
        elif len(parts) == 3:
            val = int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
        else:
            raise ValueError("too many colons")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} '{text}' (use HH:MM:SS, MM:SS, or seconds)",
        ) from exc
    if not math.isfinite(val) or val < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name}: must be >= 0",
        )
    return val


def format_hms(seconds: float) -> str:
    """Format seconds as HH:MM:SS for Streamlink/yt-dlp flags."""
    total = max(0, int(round(seconds)))
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"


def _default_url_title(platform: str, url: str) -> str:
    m_tw = re.search(r"/videos/(\d+)", url)
    if platform == "twitch" and m_tw:
        return f"Twitch VOD {m_tw.group(1)}"
    m_yt = re.search(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})", url)
    if platform == "youtube" and m_yt:
        return f"YouTube VOD {m_yt.group(1)}"
    return f"{platform.capitalize()} VOD"

DEFAULT_STUDIO_OPTIONS: dict[str, Any] = {
    "clip_count": 5,
    "min_len": 25,
    "max_len": 60,
    "min_score": 5.0,
    "profile": "balanced",
    "focus_brief": "",
    "cam_auto": True,
    "cam_box": None,
    "layout": "stack",
    "fallback_layout": "blur",
    "caption_style": "hormozi",
    "language": None,
}


def _accepts_kwarg(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)


def _validate_upload_id(upload_id: str) -> str:
    uid = str(upload_id or "").strip()
    if not _UPLOAD_ID_RE.fullmatch(uid):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid upload_id",
        )
    return uid


def _sanitize_upload_filename(filename: str, kind: str) -> tuple[str, str]:
    raw = str(filename or "").strip()
    if not raw or "/" in raw or "\\" in raw or "\x00" in raw or ".." in raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename",
        )
    ext = Path(raw).suffix.lower()
    allowed = VIDEO_EXTENSIONS if kind == "video" else CHAT_EXTENSIONS
    if ext not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported {kind} file extension '{ext or '(none)'}'",
        )
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw).stem).strip("._") or "upload"
    return f"{safe_stem}{ext}", ext


def _normalize_studio_options(
    base: dict[str, Any] | None = None,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(DEFAULT_STUDIO_OPTIONS)
    if isinstance(base, dict):
        for k, v in base.items():
            if v is not None or k in ("cam_box", "language"):
                merged[k] = v
    if isinstance(override, dict):
        for k, v in override.items():
            if v is not None or k in ("cam_box", "language"):
                merged[k] = v

    try:
        merged["clip_count"] = max(1, min(15, int(merged.get("clip_count") or 5)))
    except (TypeError, ValueError):
        merged["clip_count"] = 5
    try:
        merged["min_len"] = max(5, min(300, int(float(merged.get("min_len") or 25))))
    except (TypeError, ValueError):
        merged["min_len"] = 25
    try:
        merged["max_len"] = max(merged["min_len"], min(300, int(float(merged.get("max_len") or 60))))
    except (TypeError, ValueError):
        merged["max_len"] = max(merged["min_len"], 60)
    try:
        merged["min_score"] = max(0.0, min(10.0, float(merged.get("min_score") if merged.get("min_score") is not None else 5.0)))
    except (TypeError, ValueError):
        merged["min_score"] = 5.0

    profile = str(merged.get("profile") or "balanced").strip().lower()
    if profile not in ("balanced", "action", "dialogue"):
        profile = "balanced"
    merged["profile"] = profile
    merged["focus_brief"] = str(merged.get("focus_brief") or "").strip()
    merged["cam_auto"] = bool(merged.get("cam_auto", True))
    merged["layout"] = str(merged.get("layout") or "stack").strip().lower()
    merged["fallback_layout"] = str(merged.get("fallback_layout") or "blur").strip().lower()
    merged["caption_style"] = str(merged.get("caption_style") or "hormozi").strip().lower()
    lang = merged.get("language")
    merged["language"] = str(lang).strip() if lang else None
    return merged


async def extract_studio_preview(video_path: Path, dest_jpg: Path, duration_sec: float | None) -> bool:
    """Extract a 16:9 JPEG preview frame at min(15.0, duration * 0.2)."""
    dest_jpg.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = find_binary("ffmpeg")
    dur = float(duration_sec or 0.0)
    target_seek = min(15.0, max(0.0, dur * 0.2)) if dur > 0 else 0.0
    seeks = [f"{target_seek:.2f}", "0"] if target_seek > 0.5 else ["0"]
    for seek in seeks:
        proc = await create_subprocess_exec(
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-ss",
            seek,
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(dest_jpg),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            continue
        if proc.returncode == 0 and dest_jpg.is_file() and dest_jpg.stat().st_size > 0:
            return True
    dest_jpg.unlink(missing_ok=True)
    return False


def _studio_draft_filename(project_id: int, dt: datetime, draft_number: int, ext: str = "mp4") -> str:
    date_part = dt.strftime("%Y-%m-%d_%H%M%S")
    clean_ext = ext.lstrip(".") or "mp4"
    return f"studio_{int(project_id)}_{date_part}_draft_{int(draft_number)}.{clean_ext}"


def serialize_studio_project(
    row: aiosqlite.Row,
    *,
    include_timeline: bool = True,
) -> dict[str, Any]:
    """Convert a studio_projects row into the REST StudioProject JSON contract."""
    data = dict(row)
    try:
        options = json.loads(data.get("options_json") or "{}")
        if not isinstance(options, dict):
            options = {}
    except (TypeError, ValueError):
        options = {}

    timeline_curve = None
    if include_timeline and data.get("timeline_json"):
        try:
            timeline_curve = json.loads(data["timeline_json"])
        except (TypeError, ValueError):
            timeline_curve = None

    candidates: list[dict[str, Any]] = []
    if data.get("candidates_json"):
        try:
            parsed_cands = json.loads(data["candidates_json"])
            if isinstance(parsed_cands, list):
                candidates = parsed_cands
        except (TypeError, ValueError):
            candidates = []

    facecam = None
    if data.get("facecam_json"):
        try:
            facecam = json.loads(data["facecam_json"])
        except (TypeError, ValueError):
            facecam = None

    preview_path = data.get("preview_path")
    chat_path = data.get("chat_path")
    return {
        "id": int(data["id"]),
        "title": str(data["title"]),
        "source_type": str(data["source_type"]),
        "source_path": str(data["source_path"]),
        "chunk_id": data.get("chunk_id"),
        "streamer_id": data.get("streamer_id"),
        "streamer_name": data.get("streamer_name"),
        "duration_sec": data.get("duration_sec"),
        "has_preview": bool(preview_path and Path(str(preview_path)).is_file()),
        "has_chat": bool(chat_path and Path(str(chat_path)).is_file()),
        "options": options,
        "status": str(data.get("status") or "ready"),
        "progress": float(data.get("progress") or 0.0),
        "stage": str(data.get("stage") or ""),
        "timeline_curve": timeline_curve,
        "candidates": candidates,
        "facecam": facecam,
        "last_error": data.get("last_error"),
        "created_at": str(data["created_at"]),
        "updated_at": str(data["updated_at"]),
    }


def validate_candidates(
    raw_candidates: list[dict[str, Any]],
    duration_sec: float | None,
) -> list[dict[str, Any]]:
    """Validate and normalize Studio candidate intervals against project duration."""
    max_end = (float(duration_sec) + 0.5) if (duration_sec is not None and float(duration_sec) > 0) else None
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for idx, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Candidate #{idx} must be an object",
            )
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Candidate #{idx} requires numeric start and end",
            )
        if not (math.isfinite(start) and math.isfinite(end)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Candidate #{idx} start/end must be finite",
            )
        if start < 0.0 or start >= end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Candidate #{idx} must satisfy 0 <= start < end (got {start}..{end})",
            )
        if max_end is not None and end > max_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Candidate #{idx} end ({end:.2f}s) exceeds video duration ({duration_sec:.2f}s)",
            )

        cand_id = str(raw.get("id") or f"c_{idx:02d}").strip() or f"c_{idx:02d}"
        if cand_id in seen_ids:
            cand_id = f"{cand_id}_{idx}"
        seen_ids.add(cand_id)

        item = dict(raw)
        item["id"] = cand_id
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        item["duration"] = round(end - start, 3)
        item["title"] = str(raw.get("title") or f"Clip {idx}")
        item["selected"] = bool(raw.get("selected", True))
        if item.get("orig_start") is None:
            item["orig_start"] = item["start"]
        if item.get("orig_end") is None:
            item["orig_end"] = item["end"]
        normalized.append(item)

    return normalized


class StudioService:
    """Orchestrates VOD Studio uploads, project lifecycle, scouting, and clip export."""

    def __init__(
        self,
        settings: Settings,
        *,
        gpu_lock: asyncio.Lock | None = None,
        neon_client: NeonClippingClient | None = None,
        copywriter: Any | None = None,
        google_drive: Any | None = None,
    ) -> None:
        self.settings = settings
        self._gpu_lock = gpu_lock or asyncio.Lock()
        self.neon_client = neon_client or NeonClippingClient(
            base_url=settings.neonclipping_url,
            data_dir=settings.neonclipping_data_dir,
        )
        if copywriter is None:
            from app.services.copywriter import CopywriterService

            copywriter = CopywriterService(settings)
        if google_drive is None:
            from app.services.google_drive import GoogleDriveService

            google_drive = GoogleDriveService(settings)
        self.copywriter = copywriter
        self.google_drive = google_drive
        self._tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def gpu_lock(self) -> asyncio.Lock:
        return self._gpu_lock

    async def stop(self) -> None:
        """Cancel any active Studio background tasks cleanly at shutdown."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Chunked Uploads
    # ------------------------------------------------------------------

    def init_upload(self, filename: str, total_size: int, kind: str) -> dict[str, Any]:
        """Initialize a chunked upload session under Clips_Store\\studio\\_uploads."""
        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in ("video", "chat"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="kind must be 'video' or 'chat'",
            )
        if int(total_size) < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="total_size must be non-negative",
            )
        safe_name, ext = _sanitize_upload_filename(filename, clean_kind)
        studio_root = ensure_studio_dir(self.settings)
        upload_id = uuid.uuid4().hex
        temp_dir = resolve_safe_path(
            studio_root / "_uploads" / upload_id,
            studio_root / "_uploads",
        )
        temp_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "upload_id": upload_id,
            "filename": safe_name,
            "original_filename": filename,
            "total_size": int(total_size),
            "kind": clean_kind,
            "ext": ext,
        }
        (temp_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return {"upload_id": upload_id, "chunk_size": UPLOAD_CHUNK_SIZE}

    async def write_upload_chunk(
        self,
        upload_id: str,
        index: int,
        stream: AsyncIterator[bytes],
    ) -> dict[str, Any]:
        """Stream one binary chunk to disk for an in-progress upload."""
        uid = _validate_upload_id(upload_id)
        if index < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="index must be >= 0")
        studio_root = ensure_studio_dir(self.settings)
        temp_dir = resolve_safe_path(studio_root / "_uploads" / uid, studio_root / "_uploads")
        if not (temp_dir / "meta.json").is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")

        part_path = temp_dir / f"chunk_{index:06d}.part"
        bytes_written = 0
        with open(part_path, "wb") as fh:
            async for piece in stream:
                if piece:
                    fh.write(piece)
                    bytes_written += len(piece)
        return {"ok": True, "index": index, "bytes_written": bytes_written}

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        """Concatenate uploaded chunks in index order into Clips_Store\\studio\\uploads."""
        uid = _validate_upload_id(upload_id)
        studio_root = ensure_studio_dir(self.settings)
        temp_dir = resolve_safe_path(studio_root / "_uploads" / uid, studio_root / "_uploads")
        meta_path = temp_dir / "meta.json"
        if not meta_path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        parts_with_idx: list[tuple[int, Path]] = []
        for p in temp_dir.glob("chunk_*.part"):
            try:
                idx = int(p.stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            parts_with_idx.append((idx, p))
        parts_with_idx.sort(key=lambda item: item[0])

        expected = list(range(len(parts_with_idx)))
        if [idx for idx, _ in parts_with_idx] != expected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing or non-contiguous upload chunks",
            )
        if int(meta.get("total_size") or 0) > 0 and not parts_with_idx:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No chunks uploaded",
            )

        uploads_dir = studio_root / "uploads"
        dest_name = f"{uid}_{meta['filename']}"
        dest_path = resolve_safe_path(uploads_dir / dest_name, uploads_dir)
        with open(dest_path, "wb") as dst_fh:
            for _, part_path in parts_with_idx:
                with open(part_path, "rb") as src_fh:
                    shutil.copyfileobj(src_fh, dst_fh, 4 * 1024 * 1024)

        shutil.rmtree(temp_dir, ignore_errors=True)
        return {
            "upload_id": uid,
            "file_path": str(dest_path),
            "filename": dest_path.name,
            "size_bytes": dest_path.stat().st_size,
        }

    def _find_uploaded_file(self, upload_id: str) -> Path:
        uid = _validate_upload_id(upload_id)
        studio_root = ensure_studio_dir(self.settings)
        uploads_dir = studio_root / "uploads"
        matches = sorted(uploads_dir.glob(f"{uid}_*"))
        for m in matches:
            if m.is_file():
                return resolve_safe_path(m, uploads_dir)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Uploaded file for upload_id '{uid}' not found",
        )

    # ------------------------------------------------------------------
    # Projects CRUD
    # ------------------------------------------------------------------

    async def fetch_project_row(self, conn: aiosqlite.Connection, project_id: int) -> aiosqlite.Row | None:
        async with conn.execute(
            """
            SELECT sp.*, s.name AS streamer_name
            FROM studio_projects sp
            LEFT JOIN streamers s ON s.id = sp.streamer_id
            WHERE sp.id = ?
            """,
            (int(project_id),),
        ) as cur:
            return await cur.fetchone()

    async def list_projects(self, conn: aiosqlite.Connection) -> list[dict[str, Any]]:
        async with conn.execute(
            """
            SELECT sp.*, s.name AS streamer_name
            FROM studio_projects sp
            LEFT JOIN streamers s ON s.id = sp.streamer_id
            ORDER BY sp.id DESC
            """
        ) as cur:
            rows = await cur.fetchall()
        return [serialize_studio_project(row, include_timeline=False) for row in rows]

    async def create_project(
        self,
        conn: aiosqlite.Connection,
        *,
        title: str | None = None,
        source_type: str,
        upload_id: str | None = None,
        file_path: str | None = None,
        chunk_id: int | None = None,
        streamer_id: int | None = None,
        chat_upload_id: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        studio_root = ensure_studio_dir(self.settings)
        raw_root = Path(self.settings.raw_recordings_dir).resolve()

        resolved_source: Path
        resolved_chunk_id: int | None = None
        resolved_streamer_id: int | None = streamer_id
        resolved_chat_path: Path | None = None
        default_title: str

        if source_type == "upload":
            if upload_id:
                resolved_source = self._find_uploaded_file(upload_id)
                stem = resolved_source.stem
                if stem.startswith(f"{upload_id}_"):
                    stem = stem[len(upload_id) + 1 :]
                default_title = stem or resolved_source.name
            elif file_path:
                try:
                    resolved_source = resolve_safe_path(file_path, studio_root)
                except ValueError as exc:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
                if not resolved_source.is_file():
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded VOD file not found")
                default_title = resolved_source.stem
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="upload_id or file_path is required when source_type='upload'",
                )
        elif source_type == "chunk":
            if chunk_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="chunk_id is required when source_type='chunk'",
                )
            async with conn.execute(
                """
                SELECT rc.*, s.name AS streamer_name
                FROM recording_chunks rc
                LEFT JOIN streamers s ON s.id = rc.streamer_id
                WHERE rc.id = ?
                """,
                (int(chunk_id),),
            ) as cur:
                chunk_row = await cur.fetchone()
            if chunk_row is None or chunk_row["status"] == "deleted":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording chunk not found")
            try:
                resolved_source = resolve_safe_path(chunk_row["file_path"], raw_root, studio_root)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
            if not resolved_source.is_file():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording chunk file missing on disk")
            resolved_chunk_id = int(chunk_row["id"])
            if resolved_streamer_id is None:
                resolved_streamer_id = int(chunk_row["streamer_id"])
            sibling_chat = resolved_source.with_name(f"{resolved_source.stem}.chat.jsonl")
            if sibling_chat.is_file():
                resolved_chat_path = sibling_chat
            s_name = str(chunk_row["streamer_name"] or "Stream")
            default_title = f"{s_name} · Chunk #{int(chunk_row['chunk_index'])}"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_type must be 'upload' or 'chunk'",
            )

        if chat_upload_id:
            resolved_chat_path = self._find_uploaded_file(chat_upload_id)

        streamer_opts: dict[str, Any] = {}
        if resolved_streamer_id is not None:
            async with conn.execute(
                "SELECT id, clip_opts_json FROM streamers WHERE id = ?",
                (int(resolved_streamer_id),),
            ) as cur:
                s_row = await cur.fetchone()
            if s_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")
            try:
                streamer_opts = json.loads(s_row["clip_opts_json"] or "{}")
            except (TypeError, ValueError):
                streamer_opts = {}

        merged_options = _normalize_studio_options(streamer_opts, options)

        try:
            duration_sec = await probe_duration(resolved_source)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not probe video duration: {exc}",
            ) from exc

        clean_title = (title or "").strip() or default_title
        now = utcnow_iso()
        cur = await conn.execute(
            """
            INSERT INTO studio_projects (
                title, source_type, source_path, chunk_id, streamer_id,
                duration_sec, preview_path, chat_path, options_json,
                status, progress, stage, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'ready', 0.0, '', ?, ?)
            """,
            (
                clean_title,
                source_type,
                str(resolved_source),
                resolved_chunk_id,
                resolved_streamer_id,
                float(duration_sec),
                str(resolved_chat_path) if resolved_chat_path else None,
                json.dumps(merged_options),
                now,
                now,
            ),
        )
        await conn.commit()
        project_id = int(cur.lastrowid)

        preview_jpg = studio_root / "previews" / f"project_{project_id}.jpg"
        try:
            ok_preview = await extract_studio_preview(resolved_source, preview_jpg, duration_sec)
        except Exception as exc:
            logger.warning("Studio preview extraction failed for project %s: %s", project_id, exc)
            ok_preview = False

        if ok_preview:
            await conn.execute(
                "UPDATE studio_projects SET preview_path = ?, updated_at = ? WHERE id = ?",
                (str(preview_jpg), utcnow_iso(), project_id),
            )
            await conn.commit()

        row = await self.fetch_project_row(conn, project_id)
        assert row is not None
        return serialize_studio_project(row, include_timeline=True)

    async def create_project_from_url(
        self,
        conn: aiosqlite.Connection,
        *,
        url: str,
        title: str | None = None,
        streamer_id: int | None = None,
        downloader: str = "auto",
        quality: str = "1080p",
        start_time: str | float | int | None = None,
        end_time: str | float | int | None = None,
        fetch_chat: bool = True,
        auto_analyze: bool = False,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a Studio project backed by a Twitch or YouTube VOD URL download."""
        platform, clean_url = parse_vod_url(url)
        dl_mode = str(downloader or "auto").strip().lower()
        if dl_mode not in ("auto", "streamlink", "yt-dlp"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="downloader must be 'auto', 'streamlink', or 'yt-dlp'",
            )
        qual_mode = str(quality or "1080p").strip().lower()
        if qual_mode not in ("1080p", "720p"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="quality must be '1080p' or '720p'",
            )
        start_sec = parse_time_offset(start_time, "start_time")
        end_sec = parse_time_offset(end_time, "end_time")
        if start_sec is not None and end_sec is not None and end_sec <= start_sec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="end_time must be greater than start_time",
            )

        streamer_opts: dict[str, Any] = {}
        if streamer_id is not None:
            async with conn.execute(
                "SELECT id, clip_opts_json FROM streamers WHERE id = ?",
                (int(streamer_id),),
            ) as cur:
                s_row = await cur.fetchone()
            if s_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")
            try:
                streamer_opts = json.loads(s_row["clip_opts_json"] or "{}")
            except (TypeError, ValueError):
                streamer_opts = {}

        merged_options = _normalize_studio_options(streamer_opts, options)
        merged_options["source_url"] = clean_url
        merged_options["source_platform"] = platform
        merged_options["downloader"] = dl_mode
        merged_options["quality"] = qual_mode
        merged_options["fetch_chat"] = bool(fetch_chat)
        merged_options["auto_analyze"] = bool(auto_analyze)
        if start_sec is not None:
            merged_options["download_start_sec"] = start_sec
        if end_sec is not None:
            merged_options["download_end_sec"] = end_sec

        studio_root = ensure_studio_dir(self.settings)
        downloads_dir = studio_root / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        dest_mp4 = (downloads_dir / f"vod_{uuid.uuid4().hex[:12]}.mp4").resolve()

        clean_title = (title or "").strip() or _default_url_title(platform, clean_url)
        now = utcnow_iso()
        cur = await conn.execute(
            """
            INSERT INTO studio_projects (
                title, source_type, source_path, chunk_id, streamer_id,
                duration_sec, preview_path, chat_path, options_json,
                status, progress, stage, created_at, updated_at
            )
            VALUES (?, 'upload', ?, NULL, ?, NULL, NULL, NULL, ?, 'downloading', 0.05, 'Starting VOD download', ?, ?)
            """,
            (
                clean_title,
                str(dest_mp4),
                int(streamer_id) if streamer_id is not None else None,
                json.dumps(merged_options),
                now,
                now,
            ),
        )
        await conn.commit()
        project_id = int(cur.lastrowid)

        task = asyncio.create_task(
            self._run_download_task(
                project_id=project_id,
                platform=platform,
                url=clean_url,
                dest_mp4=dest_mp4,
                downloader=dl_mode,
                quality=qual_mode,
                start_sec=start_sec,
                end_sec=end_sec,
                fetch_chat=bool(fetch_chat),
                auto_analyze=bool(auto_analyze),
            ),
            name=f"studio-download-{project_id}",
        )
        self._tasks[project_id] = task

        row = await self.fetch_project_row(conn, project_id)
        assert row is not None
        return serialize_studio_project(row, include_timeline=True)

    async def _set_project_progress(
        self,
        project_id: int,
        *,
        progress: float,
        stage: str,
        status_value: str | None = None,
    ) -> None:
        conn = await open_connection(self.settings.db_path)
        try:
            if status_value is not None:
                await conn.execute(
                    "UPDATE studio_projects SET status = ?, progress = ?, stage = ?, updated_at = ? WHERE id = ?",
                    (status_value, float(progress), stage, utcnow_iso(), project_id),
                )
            else:
                await conn.execute(
                    "UPDATE studio_projects SET progress = ?, stage = ?, updated_at = ? WHERE id = ?",
                    (float(progress), stage, utcnow_iso(), project_id),
                )
            await conn.commit()
        finally:
            await conn.close()

    async def _download_with_streamlink(
        self,
        url: str,
        dest_mp4: Path,
        *,
        project_id: int,
        quality: str = "1080p",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> None:
        """Download a VOD using Streamlink (8 segment threads, 60fps preferred) piped to FFmpeg remux."""
        ffmpeg_bin = find_binary("ffmpeg")
        sl_cmd = [
            sys.executable,
            "-m",
            "streamlink",
            "--stdout",
            "--stream-segment-threads",
            "8",
        ]
        if start_sec is not None and start_sec > 0:
            sl_cmd.extend(["--hls-start-offset", format_hms(start_sec)])
        if end_sec is not None:
            dur = end_sec - (start_sec or 0.0)
            if dur > 0:
                sl_cmd.extend(["--hls-duration", format_hms(dur)])
        sl_quality = "720p60,720p,best" if quality == "720p" else "1080p60,best"
        sl_cmd.extend([url, sl_quality])

        ff_cmd = [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest_mp4),
        ]

        dest_mp4.parent.mkdir(parents=True, exist_ok=True)
        await self._set_project_progress(
            project_id,
            progress=0.12,
            stage="Downloading VOD via Streamlink…",
        )

        # Write stderr to temp files so a 6-hour download never deadlocks on a 64 KB pipe buffer.
        with tempfile.TemporaryDirectory(prefix="sl_dl_") as log_dir:
            sl_log = Path(log_dir) / "streamlink.err"
            ff_log = Path(log_dir) / "ffmpeg.err"
            with sl_log.open("wb") as sl_err_fh, ff_log.open("wb") as ff_err_fh:
                sl_proc = subprocess.Popen(
                    sl_cmd,
                    stdout=subprocess.PIPE,
                    stderr=sl_err_fh,
                )
                ff_proc = subprocess.Popen(
                    ff_cmd,
                    stdin=sl_proc.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=ff_err_fh,
                )
                if sl_proc.stdout is not None:
                    sl_proc.stdout.close()

                try:
                    while True:
                        sl_done = sl_proc.poll() is not None
                        ff_done = ff_proc.poll() is not None
                        if sl_done and ff_done:
                            break
                        if dest_mp4.is_file():
                            mb = dest_mp4.stat().st_size / (1024 * 1024)
                            prog = min(0.78, 0.15 + 0.63 * (1.0 - math.exp(-mb / 500.0)))
                            await self._set_project_progress(
                                project_id,
                                progress=round(prog, 3),
                                stage=f"Downloading via Streamlink ({mb:.1f} MB)",
                            )
                        await asyncio.sleep(1.5)
                except asyncio.CancelledError:
                    for p in (sl_proc, ff_proc):
                        if p.poll() is None:
                            p.kill()
                    dest_mp4.unlink(missing_ok=True)
                    raise
                finally:
                    for p in (sl_proc, ff_proc):
                        if p.poll() is None:
                            try:
                                p.kill()
                            except OSError:
                                pass

            sl_err = sl_log.read_text(encoding="utf-8", errors="replace").strip() if sl_log.is_file() else ""
            ff_err = ff_log.read_text(encoding="utf-8", errors="replace").strip() if ff_log.is_file() else ""
            if sl_proc.returncode != 0 or ff_proc.returncode != 0 or not dest_mp4.is_file() or dest_mp4.stat().st_size == 0:
                dest_mp4.unlink(missing_ok=True)
                err_msg = sl_err or ff_err or f"streamlink rc={sl_proc.returncode}, ffmpeg rc={ff_proc.returncode}"
                raise RuntimeError(f"Streamlink download failed: {err_msg[-400:]}")

    async def _download_with_ytdlp(
        self,
        url: str,
        dest_mp4: Path,
        *,
        project_id: int,
        quality: str = "1080p",
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> None:
        """Download a Twitch/YouTube VOD via yt-dlp (-N 8 concurrent fragments, 60fps preferred)."""
        ytdlp_bin = shutil.which("yt-dlp")
        cmd = [ytdlp_bin] if ytdlp_bin else [sys.executable, "-m", "yt_dlp"]
        max_h = 720 if quality == "720p" else 1080
        fmt_selector = (
            f"bv*[height<={max_h}][fps>=50][ext=mp4]+ba[ext=m4a]"
            f"/bv*[height<={max_h}][fps>=50]+ba"
            f"/bv*[height<={max_h}][ext=mp4]+ba[ext=m4a]"
            f"/bv*[height<={max_h}]+ba"
            f"/b[height<={max_h}]/best"
        )
        cmd.extend(
            [
                "--newline",
                "--no-playlist",
                "-N",
                "8",
                "-f",
                fmt_selector,
                "--merge-output-format",
                "mp4",
                "--postprocessor-args",
                "ffmpeg:-movflags +faststart",
            ]
        )
        cookies = (self.settings.youtube_cookies_file or "").strip()
        if cookies and Path(cookies).is_file():
            cmd.extend(["--cookies", cookies])
        if start_sec is not None or end_sec is not None:
            s_part = format_hms(start_sec or 0.0)
            e_part = format_hms(end_sec) if end_sec is not None else "inf"
            cmd.extend(["--download-sections", f"*{s_part}-{e_part}"])
        cmd.extend(["-o", str(dest_mp4), url])

        dest_mp4.parent.mkdir(parents=True, exist_ok=True)
        await self._set_project_progress(
            project_id,
            progress=0.12,
            stage="Downloading VOD via yt-dlp…",
        )

        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        tail_lines: list[str] = []
        last_pct_reported = -1
        last_ff_update = 0.0
        target_dur = (
            (float(end_sec) - float(start_sec or 0.0))
            if (end_sec is not None and float(end_sec) > float(start_sec or 0.0))
            else None
        )
        buf = ""

        async def _handle_line(line: str) -> None:
            nonlocal last_pct_reported, last_ff_update
            clean = line.strip()
            if not clean:
                return
            tail_lines.append(clean)
            if len(tail_lines) > 20:
                tail_lines.pop(0)

            m = _YTDLP_PCT_RE.search(clean)
            if m:
                pct = float(m.group(1))
                pct_int = int(pct)
                if pct_int >= last_pct_reported + 3 or pct_int >= 100:
                    last_pct_reported = pct_int
                    mapped = round(0.12 + 0.68 * min(100.0, pct) / 100.0, 3)
                    await self._set_project_progress(
                        project_id,
                        progress=mapped,
                        stage=f"Downloading VOD ({pct:.0f}%)",
                    )
                return

            # yt-dlp --download-sections invokes ffmpeg which outputs \r-delimited time=HH:MM:SS progress.
            mf = _FFMPEG_TIME_RE.search(clean)
            if mf:
                now_mono = asyncio.get_running_loop().time()
                if now_mono - last_ff_update < 2.0:
                    return
                last_ff_update = now_mono
                sec_done = int(mf.group(1)) * 3600.0 + int(mf.group(2)) * 60.0 + float(mf.group(3))
                if target_dur and target_dur > 0:
                    pct = min(100.0, max(0.0, (sec_done / target_dur) * 100.0))
                    mapped = round(0.12 + 0.68 * pct / 100.0, 3)
                    await self._set_project_progress(
                        project_id,
                        progress=mapped,
                        stage=f"Downloading section ({pct:.0f}% · {format_hms(sec_done)} / {format_hms(target_dur)})",
                    )
                else:
                    await self._set_project_progress(
                        project_id,
                        progress=0.45,
                        stage=f"Downloading VOD ({format_hms(sec_done)})",
                    )

        try:
            assert proc.stdout is not None
            is_async_reader = inspect.iscoroutinefunction(getattr(proc.stdout, "read", None))
            # Read fixed 4 KB chunks (never readline) so \r-only ffmpeg progress can never hit 64 KB LimitOverrunError.
            while True:
                chunk = (
                    await proc.stdout.read(4096)
                    if is_async_reader
                    else await asyncio.to_thread(proc.stdout.read, 4096)
                )
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                if "\r" in buf or "\n" in buf:
                    parts = re.split(r"[\r\n]+", buf)
                    buf = parts.pop()
                    for part in parts:
                        await _handle_line(part)
                elif len(buf) > 4096:
                    buf = buf[-2048:]
            if buf:
                await _handle_line(buf)

            rc = await proc.wait()
            if rc != 0 or not dest_mp4.is_file() or dest_mp4.stat().st_size == 0:
                dest_mp4.unlink(missing_ok=True)
                raise RuntimeError(f"yt-dlp download failed (exit {rc}): {' | '.join(tail_lines[-4:])[-400:]}")
        except asyncio.CancelledError:
            _kill_pid_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass
            _cleanup_download_partials(dest_mp4)
            raise
        finally:
            if proc.returncode is None:
                _kill_pid_tree(proc)

    async def _fetch_vod_chat(
        self,
        platform: str,
        url: str,
        dest_chat_jsonl: Path,
        *,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> Path | None:
        """Best-effort fetch of Twitch/YouTube VOD chat replay into normalized JSONL."""
        offset = float(start_sec or 0.0)
        max_rel = (float(end_sec) - offset) if (end_sec is not None) else None

        def _fetch_with_chat_downloader() -> int:
            from chat_downloader import ChatDownloader

            kwargs: dict[str, Any] = {}
            if start_sec is not None:
                kwargs["start_time"] = format_hms(start_sec)
            if end_sec is not None:
                kwargs["end_time"] = format_hms(end_sec)
            chat = ChatDownloader().get_chat(url, **kwargs)
            written = 0
            with dest_chat_jsonl.open("w", encoding="utf-8") as fh:
                for msg in chat:
                    if not isinstance(msg, dict):
                        continue
                    t_raw = msg.get("time_in_seconds")
                    if t_raw is None:
                        continue
                    try:
                        t_rel = float(t_raw) - offset
                    except (TypeError, ValueError):
                        continue
                    if t_rel < -5.0:
                        continue
                    if max_rel is not None and t_rel > max_rel + 15.0:
                        break
                    author_obj = msg.get("author")
                    author = (
                        author_obj.get("name")
                        if isinstance(author_obj, dict)
                        else str(author_obj or "")
                    )
                    text = str(msg.get("message") or "").strip()
                    if not text:
                        continue
                    rec = {
                        "t": round(max(0.0, t_rel), 2),
                        "author": str(author or ""),
                        "text": text,
                        "kind": "message",
                    }
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    written += 1
                    if written >= 250_000:
                        break
            return written

        try:
            dest_chat_jsonl.parent.mkdir(parents=True, exist_ok=True)
            if platform == "youtube":
                # Try yt-dlp live_chat first (fast, reliable for YouTube stream replays)
                with tempfile.TemporaryDirectory(prefix="yt_chat_") as tmp:
                    tmp_dir = Path(tmp)
                    ytdlp_bin = shutil.which("yt-dlp")
                    cmd = [ytdlp_bin] if ytdlp_bin else [sys.executable, "-m", "yt_dlp"]
                    cmd.extend(
                        [
                            "--skip-download",
                            "--no-playlist",
                            "--write-subs",
                            "--sub-langs",
                            "live_chat",
                            "-o",
                            str(tmp_dir / "chat"),
                            url,
                        ]
                    )
                    cookies = (self.settings.youtube_cookies_file or "").strip()
                    if cookies and Path(cookies).is_file():
                        cmd.extend(["--cookies", cookies])
                    proc = await create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=180.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()

                    live_files = list(tmp_dir.glob("*.live_chat.json"))
                    if live_files:
                        written = 0
                        with live_files[0].open("r", encoding="utf-8", errors="replace") as src_fh, dest_chat_jsonl.open("w", encoding="utf-8") as out_fh:
                            for line in src_fh:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    obj = json.loads(line)
                                except ValueError:
                                    continue
                                replay = obj.get("replayChatItemAction")
                                if not isinstance(replay, dict):
                                    continue
                                msec = replay.get("videoOffsetTimeMsec")
                                if msec is None:
                                    continue
                                try:
                                    t_rel = float(msec) / 1000.0 - offset
                                except (TypeError, ValueError):
                                    continue
                                if t_rel < -5.0:
                                    continue
                                if max_rel is not None and t_rel > max_rel + 15.0:
                                    continue
                                for action in replay.get("actions") or []:
                                    item = (action.get("addChatItemAction") or {}).get("item") or {}
                                    ren = item.get("liveChatTextMessageRenderer")
                                    if not isinstance(ren, dict):
                                        continue
                                    author = (ren.get("authorName") or {}).get("simpleText") or ""
                                    runs = (ren.get("message") or {}).get("runs") or []
                                    text_parts = []
                                    for r in runs:
                                        if "text" in r:
                                            text_parts.append(str(r["text"]))
                                        elif "emoji" in r:
                                            sc = (r["emoji"].get("shortcuts") or [""])[0]
                                            text_parts.append(str(sc))
                                    text = "".join(text_parts).strip()
                                    if not text:
                                        continue
                                    out_fh.write(
                                        json.dumps(
                                            {
                                                "t": round(max(0.0, t_rel), 2),
                                                "author": str(author),
                                                "text": text,
                                                "kind": "message",
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "\n"
                                    )
                                    written += 1
                        if written > 0:
                            return dest_chat_jsonl

            written = await asyncio.wait_for(asyncio.to_thread(_fetch_with_chat_downloader), timeout=180.0)
            if written > 0 and dest_chat_jsonl.is_file():
                return dest_chat_jsonl
            dest_chat_jsonl.unlink(missing_ok=True)
            return None
        except asyncio.CancelledError:
            dest_chat_jsonl.unlink(missing_ok=True)
            raise
        except Exception as exc:
            logger.info("Non-fatal VOD chat replay fetch skipped for %s: %s", url, exc)
            dest_chat_jsonl.unlink(missing_ok=True)
            return None

    async def _run_download_task(
        self,
        *,
        project_id: int,
        platform: str,
        url: str,
        dest_mp4: Path,
        downloader: str,
        quality: str = "1080p",
        start_sec: float | None,
        end_sec: float | None,
        fetch_chat: bool,
        auto_analyze: bool,
    ) -> None:
        try:
            if downloader == "streamlink":
                await self._download_with_streamlink(
                    url,
                    dest_mp4,
                    project_id=project_id,
                    quality=quality,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            elif downloader == "yt-dlp":
                await self._download_with_ytdlp(
                    url,
                    dest_mp4,
                    project_id=project_id,
                    quality=quality,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            else:
                # "auto": Streamlink first for Twitch (with yt-dlp fallback), yt-dlp for YouTube
                if platform == "twitch":
                    try:
                        await self._download_with_streamlink(
                            url,
                            dest_mp4,
                            project_id=project_id,
                            quality=quality,
                            start_sec=start_sec,
                            end_sec=end_sec,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as sl_exc:
                        logger.warning(
                            "Streamlink failed for %s (%s); falling back to yt-dlp",
                            url,
                            sl_exc,
                        )
                        await self._set_project_progress(
                            project_id,
                            progress=0.12,
                            stage="Streamlink failed; retrying with yt-dlp…",
                        )
                        await self._download_with_ytdlp(
                            url,
                            dest_mp4,
                            project_id=project_id,
                            quality=quality,
                            start_sec=start_sec,
                            end_sec=end_sec,
                        )
                else:
                    await self._download_with_ytdlp(
                        url,
                        dest_mp4,
                        project_id=project_id,
                        quality=quality,
                        start_sec=start_sec,
                        end_sec=end_sec,
                    )

            chat_path: Path | None = None
            if fetch_chat:
                await self._set_project_progress(
                    project_id,
                    progress=0.84,
                    stage="Fetching VOD chat replay…",
                )
                chat_path = await self._fetch_vod_chat(
                    platform,
                    url,
                    dest_mp4.with_suffix(".chat.jsonl"),
                    start_sec=start_sec,
                    end_sec=end_sec,
                )

            await self._set_project_progress(
                project_id,
                progress=0.92,
                stage="Probing video & extracting preview…",
            )
            duration_sec = await probe_duration(dest_mp4)
            studio_root = ensure_studio_dir(self.settings)
            preview_jpg = studio_root / "previews" / f"project_{project_id}.jpg"
            ok_preview = await extract_studio_preview(dest_mp4, preview_jpg, duration_sec)

            next_status = "analyzing" if auto_analyze else "ready"
            next_progress = 0.05 if auto_analyze else 1.0
            next_stage = "Queued for GPU analysis" if auto_analyze else "Download complete"

            conn = await open_connection(self.settings.db_path)
            try:
                await conn.execute(
                    """
                    UPDATE studio_projects
                    SET duration_sec = ?, preview_path = ?, chat_path = ?,
                        status = ?, progress = ?, stage = ?, last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        float(duration_sec),
                        str(preview_jpg) if ok_preview else None,
                        str(chat_path) if chat_path else None,
                        next_status,
                        next_progress,
                        next_stage,
                        utcnow_iso(),
                        project_id,
                    ),
                )
                await conn.commit()
            finally:
                await conn.close()

            if auto_analyze:
                await self._run_analyze_task(project_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Studio VOD URL download failed for project %s: %s", project_id, exc)
            conn = await open_connection(self.settings.db_path)
            try:
                await conn.execute(
                    """
                    UPDATE studio_projects
                    SET status = 'error', stage = 'Download failed', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:1000], utcnow_iso(), project_id),
                )
                await conn.commit()
            finally:
                await conn.close()
        finally:
            if self._tasks.get(project_id) is asyncio.current_task():
                self._tasks.pop(project_id, None)

    async def delete_project(self, conn: aiosqlite.Connection, project_id: int) -> dict[str, Any]:
        row = await self.fetch_project_row(conn, project_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")

        running = self._tasks.pop(int(project_id), None)
        if running is not None and not running.done():
            running.cancel()
            try:
                await asyncio.wait_for(running, timeout=8.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass

        studio_root = Path(self.settings.studio_dir).resolve()

        # Only delete uploaded media inside studio_dir; NEVER touch raw_recordings_dir or drafts_dir.
        if row["source_type"] == "upload" and row["source_path"]:
            try:
                src = resolve_safe_path(row["source_path"], studio_root)
                _cleanup_download_partials(src)
            except ValueError:
                pass

        if row["chat_path"]:
            try:
                chat_f = resolve_safe_path(row["chat_path"], studio_root)
                if chat_f.is_file():
                    chat_f.unlink(missing_ok=True)
            except ValueError:
                pass

        if row["preview_path"]:
            try:
                prev_f = resolve_safe_path(row["preview_path"], studio_root)
                if prev_f.is_file():
                    prev_f.unlink(missing_ok=True)
            except ValueError:
                pass

        await conn.execute("DELETE FROM studio_projects WHERE id = ?", (int(project_id),))
        await conn.commit()
        return {"ok": True, "id": int(project_id)}

    # ------------------------------------------------------------------
    # Analyze, Edit Candidates, Export
    # ------------------------------------------------------------------

    def _build_llm_extra_env(self) -> dict[str, str] | None:
        api_key = (self.settings.llm_api_key or "").strip()
        base_url = (self.settings.llm_base_url or "").strip()
        if not api_key or "/api/coding/v3" in base_url.lower():
            return None
        return {
            "NEONCLIPPING_LLM_BASE_URL": base_url,
            "NEONCLIPPING_LLM_MODEL": (self.settings.llm_model or "").strip(),
            "NEONCLIPPING_LLM_KEY": api_key,
        }

    async def start_analyze(
        self,
        conn: aiosqlite.Connection,
        project_id: int,
        options_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.fetch_project_row(conn, project_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")
        if row["status"] in ("downloading", "analyzing", "exporting"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project is currently {row['status']}",
            )

        existing_opts = json.loads(row["options_json"] or "{}")
        merged_opts = _normalize_studio_options(existing_opts, options_override)
        now = utcnow_iso()
        await conn.execute(
            """
            UPDATE studio_projects
            SET options_json = ?, status = 'analyzing', progress = 0.05,
                stage = 'Queued for GPU analysis', last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(merged_opts), now, int(project_id)),
        )
        await conn.commit()

        task = asyncio.create_task(
            self._run_analyze_task(int(project_id)),
            name=f"studio-analyze-{project_id}",
        )
        self._tasks[int(project_id)] = task

        updated = await self.fetch_project_row(conn, project_id)
        assert updated is not None
        return serialize_studio_project(updated, include_timeline=True)

    async def _run_analyze_task(self, project_id: int) -> None:
        try:
            async with self._gpu_lock:
                conn = await open_connection(self.settings.db_path)
                try:
                    row = await self.fetch_project_row(conn, project_id)
                    if row is None:
                        return
                    await conn.execute(
                        "UPDATE studio_projects SET progress = 0.15, stage = 'Scouting highlights & facecam', updated_at = ? WHERE id = ?",
                        (utcnow_iso(), project_id),
                    )
                    await conn.commit()
                    source_path = Path(str(row["source_path"]))
                    chat_path = Path(str(row["chat_path"])) if row["chat_path"] else None
                    options = _normalize_studio_options(json.loads(row["options_json"] or "{}"))
                finally:
                    await conn.close()

                if not source_path.is_file():
                    raise FileNotFoundError(f"VOD source file missing: {source_path}")

                neon_data_dir = Path(self.settings.neonclipping_data_dir)
                neon_data_dir.mkdir(parents=True, exist_ok=True)
                priors_file = neon_data_dir / "boundary_priors.json"

                cam_auto = bool(options.get("cam_auto", True))
                raw_cam = options.get("cam_box")
                custom_cam = raw_cam if (not cam_auto and isinstance(raw_cam, dict)) else None

                scout_kwargs: dict[str, Any] = {
                    "count": int(options["clip_count"]),
                    "min_len": float(options["min_len"]),
                    "max_len": float(options["max_len"]),
                    "min_score": float(options["min_score"]),
                    "profile": str(options["profile"]),
                    "focus_brief": str(options["focus_brief"]),
                    "cam_auto": cam_auto,
                    "cam": custom_cam,
                    "layout": str(options["layout"]),
                    "fallback_layout": str(options["fallback_layout"]),
                    "caption_style": str(options["caption_style"]),
                    "language": options.get("language"),
                    "extra_env": self._build_llm_extra_env(),
                }
                if chat_path is not None and chat_path.is_file():
                    scout_kwargs["chat_path"] = chat_path
                if priors_file.is_file():
                    scout_kwargs["boundary_priors_path"] = priors_file

                res = await self.neon_client.ascout_vod(source_path, **scout_kwargs)

                timeline_curve = res.get("timeline_curve")
                raw_cands = list(res.get("candidates") or [])
                normalized_cands: list[dict[str, Any]] = []
                for idx, c in enumerate(raw_cands, start=1):
                    item = dict(c)
                    item.setdefault("id", f"c_{idx:02d}")
                    item.setdefault("title", f"Clip {idx}")
                    item.setdefault("selected", True)
                    s = float(item["start"])
                    e = float(item["end"])
                    item.setdefault("orig_start", s)
                    item.setdefault("orig_end", e)
                    item.setdefault("duration", round(e - s, 3))
                    normalized_cands.append(item)

                facecam = res.get("facecam")
                conn = await open_connection(self.settings.db_path)
                try:
                    await conn.execute(
                        """
                        UPDATE studio_projects
                        SET timeline_json = ?, candidates_json = ?, facecam_json = ?,
                            status = 'analyzed', progress = 1.0, stage = 'Analysis complete',
                            last_error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps(timeline_curve) if timeline_curve is not None else None,
                            json.dumps(normalized_cands),
                            json.dumps(facecam) if facecam is not None else None,
                            utcnow_iso(),
                            project_id,
                        ),
                    )
                    await conn.commit()
                finally:
                    await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Studio analyze failed for project %s: %s", project_id, exc)
            conn = await open_connection(self.settings.db_path)
            try:
                await conn.execute(
                    """
                    UPDATE studio_projects
                    SET status = 'error', stage = 'Analysis failed', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:1000], utcnow_iso(), project_id),
                )
                await conn.commit()
            finally:
                await conn.close()
        finally:
            if self._tasks.get(project_id) is asyncio.current_task():
                self._tasks.pop(project_id, None)

    async def update_candidates(
        self,
        conn: aiosqlite.Connection,
        project_id: int,
        candidates: list[dict[str, Any]],
        options_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.fetch_project_row(conn, project_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")

        validated = validate_candidates(candidates, row["duration_sec"])
        now = utcnow_iso()
        if options_override is not None:
            merged_opts = _normalize_studio_options(
                json.loads(row["options_json"] or "{}"),
                options_override,
            )
            await conn.execute(
                """
                UPDATE studio_projects
                SET candidates_json = ?, options_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(validated), json.dumps(merged_opts), now, int(project_id)),
            )
        else:
            await conn.execute(
                """
                UPDATE studio_projects
                SET candidates_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(validated), now, int(project_id)),
            )
        await conn.commit()
        updated = await self.fetch_project_row(conn, project_id)
        assert updated is not None
        return serialize_studio_project(updated, include_timeline=True)

    async def start_export(
        self,
        conn: aiosqlite.Connection,
        project_id: int,
        candidate_ids: list[str] | None = None,
        options_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await self.fetch_project_row(conn, project_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")
        if row["status"] in ("downloading", "analyzing", "exporting"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project is currently {row['status']}",
            )

        all_cands = json.loads(row["candidates_json"] or "[]")
        if candidate_ids is not None:
            wanted = set(candidate_ids)
            selected = [c for c in all_cands if c.get("id") in wanted]
        else:
            selected = [c for c in all_cands if bool(c.get("selected", True))]

        if not selected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No candidates selected for export",
            )

        existing_opts = json.loads(row["options_json"] or "{}")
        merged_opts = _normalize_studio_options(existing_opts, options_override)
        now = utcnow_iso()
        await conn.execute(
            """
            UPDATE studio_projects
            SET options_json = ?, status = 'exporting', progress = 0.05,
                stage = 'Queued for GPU export', last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(merged_opts), now, int(project_id)),
        )
        await conn.commit()

        task = asyncio.create_task(
            self._run_export_task(int(project_id), selected),
            name=f"studio-export-{project_id}",
        )
        self._tasks[int(project_id)] = task

        updated = await self.fetch_project_row(conn, project_id)
        assert updated is not None
        return serialize_studio_project(updated, include_timeline=True)

    async def _resolve_export_streamer(
        self,
        conn: aiosqlite.Connection,
        streamer_id: int | None,
    ) -> dict[str, Any]:
        if streamer_id is not None:
            async with conn.execute("SELECT * FROM streamers WHERE id = ?", (int(streamer_id),)) as cur:
                row = await cur.fetchone()
            if row is not None:
                return dict(row)

        async with conn.execute("SELECT * FROM streamers ORDER BY id ASC LIMIT 1") as cur:
            row = await cur.fetchone()
        if row is not None:
            return dict(row)

        now = utcnow_iso()
        cur = await conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref, auto_watch,
                clip_opts_json, is_live, created_at, updated_at
            )
            VALUES ('Studio Upload', 'studio-upload', 'twitch',
                    'https://www.twitch.tv/studio_upload', 'studio_upload',
                    0, '{}', 0, ?, ?)
            """,
            (now, now),
        )
        await conn.commit()
        async with conn.execute("SELECT * FROM streamers WHERE id = ?", (int(cur.lastrowid),)) as s_cur:
            created = await s_cur.fetchone()
        assert created is not None
        return dict(created)

    async def _update_boundary_priors(
        self,
        conn: aiosqlite.Connection,
        neon_data_dir: Path,
    ) -> None:
        async with conn.execute(
            "SELECT category, preroll_sec, tail_sec FROM boundary_feedback"
        ) as cur:
            rows = await cur.fetchall()
        if len(rows) < 5:
            return

        by_cat_pre: dict[str, list[float]] = defaultdict(list)
        by_cat_tail: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            cat = str(r["category"] or "default")
            if r["preroll_sec"] is not None:
                val = max(4.0, min(60.0, float(r["preroll_sec"])))
                by_cat_pre[cat].append(val)
                by_cat_pre["default"].append(val)
            if r["tail_sec"] is not None:
                val = max(2.0, min(30.0, float(r["tail_sec"])))
                by_cat_tail[cat].append(val)
                by_cat_tail["default"].append(val)

        preroll_priors: dict[str, dict[str, float]] = {}
        for cat, vals in by_cat_pre.items():
            target = round(float(statistics.median(vals)), 2)
            preroll_priors[cat] = {
                "min": round(max(4.0, target * 0.65), 2),
                "target": target,
                "max": round(min(60.0, max(target + 6.0, target * 1.5)), 2),
            }

        tail_priors: dict[str, dict[str, float]] = {}
        for cat, vals in by_cat_tail.items():
            target = round(float(statistics.median(vals)), 2)
            tail_priors[cat] = {
                "min": round(max(2.0, target * 0.5), 2),
                "target": target,
                "max": round(min(30.0, max(target + 4.0, target * 1.75)), 2),
            }

        priors_payload = {
            "preroll": preroll_priors,
            "tail": tail_priors,
            "sample_count": len(rows),
            "updated_at": utcnow_iso(),
        }
        priors_path = neon_data_dir / "boundary_priors.json"
        priors_path.write_text(json.dumps(priors_payload, indent=2), encoding="utf-8")

    async def _run_export_task(
        self,
        project_id: int,
        selected_candidates: list[dict[str, Any]],
    ) -> None:
        staging_dir: Path | None = None
        try:
            async with self._gpu_lock:
                conn = await open_connection(self.settings.db_path)
                try:
                    row = await self.fetch_project_row(conn, project_id)
                    if row is None:
                        return
                    await conn.execute(
                        "UPDATE studio_projects SET progress = 0.20, stage = ?, updated_at = ? WHERE id = ?",
                        (f"Exporting {len(selected_candidates)} clip(s)", utcnow_iso(), project_id),
                    )
                    await conn.commit()
                    source_path = Path(str(row["source_path"]))
                    project_title = str(row["title"])
                    project_chunk_id = row["chunk_id"]
                    project_streamer_id = row["streamer_id"]
                    options = _normalize_studio_options(json.loads(row["options_json"] or "{}"))
                finally:
                    await conn.close()

                if not source_path.is_file():
                    raise FileNotFoundError(f"VOD source file missing: {source_path}")

                neon_data_dir = Path(self.settings.neonclipping_data_dir)
                neon_data_dir.mkdir(parents=True, exist_ok=True)
                staging_dir = Path(
                    tempfile.mkdtemp(prefix=f"studio_{project_id}_", dir=str(neon_data_dir))
                )
                clips_json_path = staging_dir / "clips.json"
                export_payload = [
                    {
                        "id": c.get("id"),
                        "start": float(c["start"]),
                        "end": float(c["end"]),
                        "title": str(c.get("title") or f"Clip {idx}"),
                        "hook": str(c.get("hook") or ""),
                        "visual_summary": str(c.get("visual_summary") or ""),
                        "score": c.get("score"),
                        "features": c.get("features") or {},
                    }
                    for idx, c in enumerate(selected_candidates, start=1)
                ]
                clips_json_path.write_text(json.dumps(export_payload), encoding="utf-8")

                cam_auto = bool(options.get("cam_auto", True))
                raw_cam = options.get("cam_box")
                custom_cam = raw_cam if (not cam_auto and isinstance(raw_cam, dict)) else None

                export_res = await self.neon_client.aexport_vod_clips(
                    source_path,
                    staging_dir,
                    clips_json_path,
                    count=len(selected_candidates),
                    min_len=float(options["min_len"]),
                    max_len=float(options["max_len"]),
                    cam_auto=cam_auto,
                    cam=custom_cam,
                    layout=str(options["layout"]),
                    fallback_layout=str(options["fallback_layout"]),
                    caption_style=str(options["caption_style"]),
                    language=options.get("language"),
                    extra_env=self._build_llm_extra_env(),
                )

                clips = list(export_res.get("clips") or [])
                drafts_dir = Path(self.settings.drafts_dir)
                drafts_dir.mkdir(parents=True, exist_ok=True)

                created_clip_ids: list[int] = []
                export_dt = datetime.now(timezone.utc)
                clip_date = export_dt.strftime("%Y-%m-%d")

                conn = await open_connection(self.settings.db_path)
                try:
                    streamer_row = await self._resolve_export_streamer(conn, project_streamer_id)
                    target_streamer_id = int(streamer_row["id"])
                    streamer_name = str(streamer_row["name"])
                    streamer_platform = str(streamer_row["platform"] or "")
                    try:
                        s_clip_opts = json.loads(streamer_row.get("clip_opts_json") or "{}")
                    except (TypeError, ValueError):
                        s_clip_opts = {}
                    hashtags = (
                        [str(t) for t in (s_clip_opts.get("hashtags") or []) if str(t).strip()]
                        if project_streamer_id is not None
                        else []
                    )

                    for idx, clip in enumerate(clips, start=1):
                        draft_number = int(clip.get("index") or idx)
                        src_mp4 = Path(str(clip["output_mp4"]))
                        if not src_mp4.is_file():
                            raise FileNotFoundError(f"Exported clip MP4 missing: {src_mp4}")

                        dest_mp4 = drafts_dir / _studio_draft_filename(
                            project_id, export_dt, draft_number, ext="mp4"
                        )
                        shutil.move(str(src_mp4), str(dest_mp4))

                        src_srt_raw = clip.get("output_srt")
                        if src_srt_raw:
                            src_srt = Path(str(src_srt_raw))
                            if src_srt.is_file():
                                dest_srt = drafts_dir / _studio_draft_filename(
                                    project_id, export_dt, draft_number, ext="srt"
                                )
                                shutil.move(str(src_srt), str(dest_srt))

                        dest_jpg = drafts_dir / _studio_draft_filename(
                            project_id, export_dt, draft_number, ext="jpg"
                        )
                        thumb_ok = await _extract_thumbnail_jpeg(dest_mp4, dest_jpg)
                        thumbnail_path = str(dest_jpg) if thumb_ok else None

                        cand_ref = (
                            selected_candidates[idx - 1]
                            if idx - 1 < len(selected_candidates)
                            else {}
                        )
                        copy_kwargs: dict[str, Any] = {
                            "transcript": str(clip.get("transcript") or cand_ref.get("transcript") or ""),
                            "neon_title": str(clip.get("title") or cand_ref.get("title") or ""),
                            "neon_hook": str(clip.get("hook") or cand_ref.get("hook") or ""),
                            "streamer_name": streamer_name,
                            "stream_title": project_title,
                            "platform": streamer_platform,
                        }
                        visual_summary = str(
                            clip.get("visual_summary") or cand_ref.get("visual_summary") or ""
                        ).strip()
                        if visual_summary or _accepts_kwarg(
                            self.copywriter.generate_youtube_copy, "visual_summary"
                        ):
                            copy_kwargs["visual_summary"] = visual_summary

                        copy_res = await self.copywriter.generate_youtube_copy(**copy_kwargs)
                        title = str(
                            copy_res.get("title") or clip.get("title") or cand_ref.get("title") or f"Clip {draft_number}"
                        )
                        description = str(copy_res.get("description") or "")
                        if hashtags:
                            description = "\n\n".join(p for p in (description, " ".join(hashtags)) if p)
                        tags = list(copy_res.get("tags") or [])
                        features = clip.get("features") or cand_ref.get("features")

                        drive_file_id: str | None = None
                        drive_url: str | None = None
                        try:
                            drive_res = await self.google_drive.upload_clip(
                                conn,
                                dest_mp4,
                                streamer_name=streamer_name,
                                clip_date=clip_date,
                                display_name=dest_mp4.name,
                                share_anyone=True,
                            )
                            if drive_res:
                                drive_file_id = drive_res.get("drive_file_id")
                                drive_url = drive_res.get("drive_url")
                        except Exception as drive_exc:
                            logger.warning(
                                "Non-fatal Google Drive upload failure for %s: %s",
                                dest_mp4.name,
                                drive_exc,
                            )

                        start_sec = float(
                            clip["start"] if clip.get("start") is not None else cand_ref.get("start", 0.0)
                        )
                        end_sec = float(
                            clip["end"] if clip.get("end") is not None else cand_ref.get("end", start_sec)
                        )
                        duration_sec = (
                            float(clip["duration"])
                            if clip.get("duration") is not None
                            else round(end_sec - start_sec, 3)
                        )
                        raw_score = clip.get("score") if clip.get("score") is not None else cand_ref.get("score")
                        score = float(raw_score) if raw_score is not None else None

                        now = utcnow_iso()
                        cur = await conn.execute(
                            """
                            INSERT INTO clip_drafts (
                                streamer_id, session_id, chunk_id, draft_number,
                                title, description, tags_json, file_path, thumbnail_path,
                                drive_file_id, drive_url, start_sec, end_sec, duration_sec,
                                score, features_json, status, created_at, updated_at
                            )
                            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                            """,
                            (
                                target_streamer_id,
                                project_chunk_id,
                                draft_number,
                                title,
                                description,
                                json.dumps(tags),
                                str(dest_mp4),
                                thumbnail_path,
                                drive_file_id,
                                drive_url,
                                start_sec,
                                end_sec,
                                duration_sec,
                                score,
                                json.dumps(features) if isinstance(features, dict) and features else None,
                                now,
                                now,
                            ),
                        )
                        await conn.commit()
                        created_clip_ids.append(int(cur.lastrowid))

                    # Record boundary_feedback for adjusted clips
                    feedback_added = False
                    for cand in selected_candidates:
                        orig_s = cand.get("orig_start")
                        orig_e = cand.get("orig_end")
                        if orig_s is None or orig_e is None:
                            continue
                        orig_s_f = float(orig_s)
                        orig_e_f = float(orig_e)
                        final_s_f = float(cand["start"])
                        final_e_f = float(cand["end"])
                        if abs(final_s_f - orig_s_f) <= 0.05 and abs(final_e_f - orig_e_f) <= 0.05:
                            continue
                        peak_raw = cand.get("peak_sec")
                        peak_f = float(peak_raw) if peak_raw is not None else None
                        preroll = round(peak_f - final_s_f, 3) if peak_f is not None else None
                        tail = round(final_e_f - peak_f, 3) if peak_f is not None else None
                        await conn.execute(
                            """
                            INSERT INTO boundary_feedback (
                                project_id, streamer_id, category, peak_sec,
                                orig_start, orig_end, final_start, final_end,
                                preroll_sec, tail_sec, created_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                project_id,
                                project_streamer_id,
                                str(cand.get("category") or "default"),
                                peak_f,
                                orig_s_f,
                                orig_e_f,
                                final_s_f,
                                final_e_f,
                                preroll,
                                tail,
                                utcnow_iso(),
                            ),
                        )
                        feedback_added = True

                    if feedback_added:
                        await conn.commit()
                        await self._update_boundary_priors(conn, neon_data_dir)

                    options["last_exported_clip_ids"] = created_clip_ids
                    await conn.execute(
                        """
                        UPDATE studio_projects
                        SET options_json = ?, status = 'exported', progress = 1.0,
                            stage = ?, last_error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps(options),
                            f"Exported {len(created_clip_ids)} clip(s)",
                            utcnow_iso(),
                            project_id,
                        ),
                    )
                    await conn.commit()
                finally:
                    await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Studio export failed for project %s: %s", project_id, exc)
            conn = await open_connection(self.settings.db_path)
            try:
                await conn.execute(
                    """
                    UPDATE studio_projects
                    SET status = 'error', stage = 'Export failed', last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:1000], utcnow_iso(), project_id),
                )
                await conn.commit()
            finally:
                await conn.close()
        finally:
            if staging_dir is not None and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            if self._tasks.get(project_id) is asyncio.current_task():
                self._tasks.pop(project_id, None)
