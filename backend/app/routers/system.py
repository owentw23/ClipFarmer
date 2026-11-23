from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config import Settings, get_settings
from app.db import get_db
from app.services.recorder import RecorderManager
from app.services.retention_gc import RetentionGC
from app.services.watcher_manager import WatcherManager

router = APIRouter(prefix="/api", tags=["system"])


def _get_recorder(request: Request, settings: Settings) -> RecorderManager:
    recorder = getattr(request.app.state, "recorder", None)
    if recorder is None:
        recorder = RecorderManager(settings)
        request.app.state.recorder = recorder
    return recorder


def _get_retention_gc(request: Request, settings: Settings) -> RetentionGC:
    gc = getattr(request.app.state, "retention_gc", None)
    if gc is None:
        gc = RetentionGC(settings)
        request.app.state.retention_gc = gc
    return gc


def _get_watcher_manager(
    request: Request,
    settings: Settings,
    recorder: RecorderManager,
) -> WatcherManager:
    wm = getattr(request.app.state, "watcher_manager", None)
    if wm is None:
        wm = WatcherManager(
            settings,
            on_online=recorder.start_recording,
            on_offline=recorder.request_stop,
            is_recording=recorder.is_recording,
            offline_confirm_polls=2,
        )
        request.app.state.watcher_manager = wm
    return wm


def _serialize_chunk(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "streamer_id": row["streamer_id"],
        "streamer_name": row["streamer_name"],
        "streamer_slug": row["streamer_slug"],
        "chunk_index": row["chunk_index"],
        "file_path": row["file_path"],
        "duration_sec": row["duration_sec"],
        "merged_with_prev": bool(row["merged_with_prev"]),
        "merged_from_chunk_id": row["merged_from_chunk_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "delete_after": row["delete_after"],
        "created_at": row["created_at"],
    }


@router.get("/system/recordings")
async def get_system_recordings(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return currently active recording sessions and recent recording chunks."""
    recorder = _get_recorder(request, settings)
    async with db.execute(
        """
        SELECT rc.*, s.name AS streamer_name, s.slug AS streamer_slug
        FROM recording_chunks rc
        JOIN streamers s ON s.id = rc.streamer_id
        ORDER BY rc.id DESC
        LIMIT 50
        """
    ) as cur:
        rows = await cur.fetchall()

    return {
        "active": recorder.active_recordings(),
        "recent_chunks": [_serialize_chunk(row) for row in rows],
    }


@router.post("/streamers/{streamer_id}/check")
async def check_streamer_now(
    streamer_id: int,
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Immediately probe a streamer's live status and start recording if live."""
    recorder = _get_recorder(request, settings)
    watcher_manager = _get_watcher_manager(request, settings, recorder)
    was_recording = recorder.is_recording(streamer_id)

    try:
        result = await watcher_manager.check_streamer(streamer_id, db_path=settings.db_path)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Streamer not found",
        ) from exc

    is_live = bool(result.get("is_live"))
    stream_info = result.get("stream")
    held = bool(result.get("held"))
    if is_live and not held and not recorder.is_recording(streamer_id) and stream_info is not None:
        await recorder.start_recording(stream_info)

    recording_started = (not was_recording) and recorder.is_recording(streamer_id)
    return {
        "streamer_id": streamer_id,
        "is_live": is_live,
        "recording_started": recording_started,
        "stream": stream_info,
        "checked_at": result.get("checked_at"),
        "held": held,
    }


@router.post("/streamers/{streamer_id}/stop")
async def stop_streamer_recording(
    streamer_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Stop recording a streamer and keep it stopped until this stream ends.

    The hold is cleared the next time the watcher sees the streamer offline, or by
    POST /api/streamers/{id}/resume. Without it, the next live check restarted the recorder.
    """
    async with db.execute("SELECT id FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Streamer not found",
        )

    await db.execute("UPDATE streamers SET hold_until_offline = 1 WHERE id = ?", (streamer_id,))
    await db.commit()

    recorder = _get_recorder(request, settings)
    stopped = await recorder.stop_recording(streamer_id)
    return {
        "ok": True,
        "streamer_id": streamer_id,
        "stopped": bool(stopped),
        "held": True,
    }


@router.post("/streamers/{streamer_id}/resume")
async def resume_streamer_recording(
    streamer_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Lift a manual stop and start recording now if the streamer is live."""
    async with db.execute("SELECT id FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")
    await db.execute("UPDATE streamers SET hold_until_offline = 0 WHERE id = ?", (streamer_id,))
    await db.commit()
    return await check_streamer_now(streamer_id, request, settings)


@router.get("/system/sessions")
async def get_recording_sessions(
    db: aiosqlite.Connection = Depends(get_db),
    limit: int = 25,
) -> dict[str, Any]:
    """Recent recording sessions with how each one ended and any error."""
    async with db.execute(
        """
        SELECT ss.id, ss.streamer_id, s.name AS streamer_name, s.platform, ss.title,
               ss.started_at, ss.ended_at, ss.status, ss.end_reason, ss.last_error, ss.log_path,
               (SELECT COUNT(*) FROM recording_chunks rc
                 WHERE rc.session_id = ss.id AND rc.status NOT IN ('discarded_short')) AS chunks
        FROM stream_sessions ss
        JOIN streamers s ON s.id = ss.streamer_id
        ORDER BY ss.id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 200)),),
    ) as cur:
        rows = await cur.fetchall()
    return {"items": [dict(row) for row in rows]}


@router.post("/system/gc/run")
async def run_retention_gc_now(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Run one retention garbage-collection pass immediately."""
    retention_gc = _get_retention_gc(request, settings)
    result = await retention_gc.run_once(db_path=settings.db_path)
    return {
        "deleted_chunk_ids": result["deleted_chunk_ids"],
        "skipped_locked": result["skipped_locked"],
    }


def _get_learning(request: Request, settings: Settings):
    from app.services.learning import LearningService

    svc = getattr(request.app.state, "learning", None)
    if svc is None:
        svc = LearningService(settings)
        request.app.state.learning = svc
    return svc


@router.get("/system/learning")
async def get_learning_summary(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """What NeonClipping's ranking has learned from posted and rejected drafts."""
    try:
        return await _get_learning(request, settings).summary()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/system/learning/sync")
async def sync_learning_now(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Send any new ratings now instead of waiting for the next background pass."""
    svc = _get_learning(request, settings)
    result = await svc.sync()
    return {**result, "summary": await svc.summary()}
