from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.config import Settings, get_settings
from app.db import get_db
from app.services.processing_queue import ProcessingQueueWorker, ensure_chunk_error_column

router = APIRouter(prefix="/api/system/queue", tags=["queue"])


def _get_processing_queue(request: Request, settings: Settings) -> ProcessingQueueWorker:
    pq = getattr(request.app.state, "processing_queue", None)
    if pq is None:
        pq = ProcessingQueueWorker(settings)
        request.app.state.processing_queue = pq
    return pq


def _serialize_queue_chunk(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "session_id": data["session_id"],
        "streamer_id": data["streamer_id"],
        "streamer_name": data.get("streamer_name"),
        "streamer_slug": data.get("streamer_slug"),
        "chunk_index": data["chunk_index"],
        "file_path": data["file_path"],
        "duration_sec": data.get("duration_sec"),
        "merged_with_prev": bool(data.get("merged_with_prev")),
        "merged_from_chunk_id": data.get("merged_from_chunk_id"),
        "status": data["status"],
        "last_error": data.get("last_error"),
        "started_at": data.get("started_at"),
        "completed_at": data.get("completed_at"),
        "delete_after": data.get("delete_after"),
        "created_at": data["created_at"],
    }


@router.get("")
async def get_queue_status(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Return GPU processing queue status and recent queueable/processed/failed chunks."""
    pq = _get_processing_queue(request, settings)
    pq_status = pq.status()

    async with db.execute(
        """
        SELECT rc.*, s.name AS streamer_name, s.slug AS streamer_slug
        FROM recording_chunks rc
        JOIN streamers s ON s.id = rc.streamer_id
        WHERE rc.status IN ('queued', 'processing', 'processed', 'failed')
        ORDER BY rc.id DESC
        LIMIT 50
        """
    ) as cur:
        rows = await cur.fetchall()

    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM recording_chunks WHERE status = 'queued'"
    ) as cnt_cur:
        cnt_row = await cnt_cur.fetchone()
    db_queued = int(cnt_row["cnt"]) if cnt_row is not None else 0

    return {
        "running_chunk_id": pq_status["running_chunk_id"],
        "paused": pq_status["paused"],
        "queued_count": max(int(pq_status["queued_count"]), db_queued),
        "chunks": [_serialize_queue_chunk(row) for row in rows],
    }


@router.post("/pause")
async def pause_queue(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    """Pause dispatching new chunks from the GPU processing queue."""
    pq = _get_processing_queue(request, settings)
    pq.pause()
    return {"paused": True}


@router.post("/resume")
async def resume_queue(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    """Resume dispatching chunks from the GPU processing queue."""
    pq = _get_processing_queue(request, settings)
    pq.resume()
    return {"paused": False}


@router.post("/process/{chunk_id}")
async def process_chunk_now(
    chunk_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Queue or retry a recording chunk for NeonClipping processing."""
    async with db.execute(
        "SELECT id FROM recording_chunks WHERE id = ?",
        (chunk_id,),
    ) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recording chunk not found",
        )

    await ensure_chunk_error_column(db)
    await db.execute(
        "UPDATE recording_chunks SET status = 'queued', last_error = NULL WHERE id = ?",
        (chunk_id,),
    )
    await db.commit()

    pq = _get_processing_queue(request, settings)
    pq.enqueue_chunk(chunk_id)
    return {
        "ok": True,
        "chunk_id": chunk_id,
        "status": "queued",
    }
