"""REST API endpoints for the VOD Studio."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import get_db
from app.services.studio_service import StudioService, serialize_studio_project
from app.storage import resolve_safe_path

router = APIRouter(prefix="/api/studio", tags=["studio"])


class InitUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(ge=0)
    kind: Literal["video", "chat"]


class CreateProjectRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    source_type: Literal["upload", "chunk"]
    upload_id: str | None = None
    file_path: str | None = None
    chunk_id: int | None = None
    streamer_id: int | None = None
    chat_upload_id: str | None = None
    options: dict[str, Any] | None = None


class CreateProjectFromUrlRequest(BaseModel):
    url: str = Field(min_length=8, max_length=1000)
    title: str | None = Field(default=None, max_length=200)
    streamer_id: int | None = None
    downloader: Literal["auto", "streamlink", "yt-dlp"] = "auto"
    quality: Literal["1080p", "720p"] = "1080p"
    start_time: str | float | int | None = None
    end_time: str | float | int | None = None
    fetch_chat: bool = True
    auto_analyze: bool = False
    options: dict[str, Any] | None = None


class AnalyzeProjectRequest(BaseModel):
    options: dict[str, Any] | None = None


class UpdateCandidatesRequest(BaseModel):
    candidates: list[dict[str, Any]]
    options: dict[str, Any] | None = None


class ExportProjectRequest(BaseModel):
    candidate_ids: list[str] | None = None
    options: dict[str, Any] | None = None


def _get_studio_service(request: Request, settings: Settings) -> StudioService:
    svc = getattr(request.app.state, "studio_service", None)
    if svc is None:
        pq = getattr(request.app.state, "processing_queue", None)
        gpu_lock = pq.gpu_lock if pq is not None else None
        svc = StudioService(
            settings,
            gpu_lock=gpu_lock,
            neon_client=getattr(request.app.state, "neon_client", None),
            copywriter=getattr(request.app.state, "copywriter", None),
            google_drive=getattr(request.app.state, "google_drive", None),
        )
        request.app.state.studio_service = svc
    return svc


@router.post("/uploads/init")
async def init_studio_upload(
    payload: InitUploadRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Initialize a chunked Studio VOD or chat upload session."""
    svc = _get_studio_service(request, settings)
    return svc.init_upload(payload.filename, payload.total_size, payload.kind)


@router.put("/uploads/{upload_id}/chunk")
async def put_studio_upload_chunk(
    upload_id: str,
    request: Request,
    index: int = Query(..., ge=0),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Receive one raw binary chunk for an active Studio upload."""
    svc = _get_studio_service(request, settings)
    return await svc.write_upload_chunk(upload_id, index, request.stream())


@router.post("/uploads/{upload_id}/complete")
async def complete_studio_upload(
    upload_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Concatenate uploaded chunks into Clips_Store\\studio\\uploads."""
    svc = _get_studio_service(request, settings)
    return svc.complete_upload(upload_id)


@router.post("/projects")
async def create_studio_project(
    payload: CreateProjectRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Create a Studio project from an uploaded VOD or recorded chunk."""
    svc = _get_studio_service(request, settings)
    return await svc.create_project(
        db,
        title=payload.title,
        source_type=payload.source_type,
        upload_id=payload.upload_id,
        file_path=payload.file_path,
        chunk_id=payload.chunk_id,
        streamer_id=payload.streamer_id,
        chat_upload_id=payload.chat_upload_id,
        options=payload.options,
    )


@router.post("/projects/from-url")
async def create_studio_project_from_url(
    payload: CreateProjectFromUrlRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Download a Twitch or YouTube VOD URL into Clips_Store\\studio\\ and open as a project."""
    svc = _get_studio_service(request, settings)
    return await svc.create_project_from_url(
        db,
        url=payload.url,
        title=payload.title,
        streamer_id=payload.streamer_id,
        downloader=payload.downloader,
        quality=payload.quality,
        start_time=payload.start_time,
        end_time=payload.end_time,
        fetch_chat=payload.fetch_chat,
        auto_analyze=payload.auto_analyze,
        options=payload.options,
    )


@router.get("/projects")
async def list_studio_projects(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List all Studio projects ordered newest first."""
    svc = _get_studio_service(request, settings)
    return {"items": await svc.list_projects(db)}


@router.get("/projects/{project_id}")
async def get_studio_project(
    project_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Fetch a single Studio project with timeline curve and candidates."""
    svc = _get_studio_service(request, settings)
    row = await svc.fetch_project_row(db, project_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")
    return serialize_studio_project(row, include_timeline=True)


@router.get("/projects/{project_id}/video")
async def get_studio_project_video(
    project_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Serve the Studio project's source VOD with HTTP 206 Range request support."""
    svc = _get_studio_service(request, settings)
    row = await svc.fetch_project_row(db, project_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")
    try:
        resolved = resolve_safe_path(
            row["source_path"],
            Path(settings.studio_dir),
            Path(settings.raw_recordings_dir),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project video file not found")
    media_type, _ = mimetypes.guess_type(str(resolved))
    return FileResponse(
        path=str(resolved),
        media_type=media_type or "video/mp4",
        filename=resolved.name,
    )


@router.get("/projects/{project_id}/preview")
async def get_studio_project_preview(
    project_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Serve the 16:9 JPEG preview frame for webcam box selection."""
    svc = _get_studio_service(request, settings)
    row = await svc.fetch_project_row(db, project_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Studio project not found")
    if not row["preview_path"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview image not available")
    try:
        resolved = resolve_safe_path(row["preview_path"], Path(settings.studio_dir))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preview image file not found")
    return FileResponse(path=str(resolved), media_type="image/jpeg", filename=resolved.name)


@router.delete("/projects/{project_id}")
async def delete_studio_project(
    project_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Delete a Studio project and any uploaded VOD/chat/preview inside studio_dir."""
    svc = _get_studio_service(request, settings)
    return await svc.delete_project(db, project_id)


@router.post("/projects/{project_id}/analyze")
async def analyze_studio_project(
    project_id: int,
    request: Request,
    payload: AnalyzeProjectRequest | None = Body(default=None),
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Start asynchronous GPU scouting (timeline curve + candidates + facecam) for a project."""
    svc = _get_studio_service(request, settings)
    return await svc.start_analyze(
        db,
        project_id,
        options_override=payload.options if payload else None,
    )


@router.patch("/projects/{project_id}/candidates")
async def update_studio_candidates(
    project_id: int,
    payload: UpdateCandidatesRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Persist user-adjusted clip intervals, titles, hooks, and selection toggles."""
    svc = _get_studio_service(request, settings)
    return await svc.update_candidates(
        db,
        project_id,
        payload.candidates,
        options_override=payload.options,
    )


@router.post("/projects/{project_id}/export")
async def export_studio_project(
    project_id: int,
    request: Request,
    payload: ExportProjectRequest | None = Body(default=None),
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Start asynchronous GPU export of selected Studio candidates into clip_drafts."""
    svc = _get_studio_service(request, settings)
    return await svc.start_export(
        db,
        project_id,
        candidate_ids=payload.candidate_ids if payload else None,
        options_override=payload.options if payload else None,
    )
