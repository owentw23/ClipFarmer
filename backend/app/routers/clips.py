from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Literal

import aiosqlite
import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import get_db, utcnow_iso
from app.services.publish_scheduler import parse_utc_datetime
from app.services.youtube_publisher import YouTubePublisherError, YouTubePublisherService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clips", tags=["clips"])
BULK_DRIVE_DELETE_INTERVAL_SEC = 0.15

ClipStatus = Literal[
    "draft",
    "notified",
    "approved",
    "scheduled",
    "kept_local",
    "posted",
    "failed",
    "archived",
    "deleted",
]
PrivacyStatus = Literal["public", "unlisted", "private"]


class UpdateClipRequest(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = None
    status: ClipStatus | None = None


class ApproveClipRequest(BaseModel):
    privacy_status: PrivacyStatus | None = None
    publish_now: bool = True
    youtube_account_ids: list[int] | None = None


class PublishClipRequest(BaseModel):
    privacy_status: PrivacyStatus | None = None
    youtube_account_ids: list[int] | None = None


class BulkApproveClipsRequest(BaseModel):
    clip_ids: list[int] = Field(min_length=1, max_length=5000)
    privacy_status: PrivacyStatus | None = None
    publish_now: bool = True
    youtube_account_ids: list[int] | None = None


class BulkRejectClipsRequest(BaseModel):
    clip_ids: list[int] = Field(min_length=1, max_length=5000)


class BulkDeleteClipsRequest(BaseModel):
    clip_ids: list[int] = Field(min_length=1, max_length=5000)
    delete_drive: bool = False


class ScheduleClipRequest(BaseModel):
    scheduled_at: str = Field(min_length=10, max_length=64)
    privacy_status: PrivacyStatus | None = None


class BulkScheduleClipsRequest(BaseModel):
    clip_ids: list[int] = Field(min_length=1, max_length=5000)
    scheduled_at: str = Field(min_length=10, max_length=64)
    interval_minutes: int = Field(default=0, ge=0, le=10080)
    privacy_status: PrivacyStatus | None = None


def _parse_future_schedule_utc(raw: str) -> datetime:
    dt = parse_utc_datetime(raw)
    if dt is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid scheduled_at timestamp; use ISO-8601 format",
        )
    # Allow 60s of clock/submission skew while preventing accidental past schedules.
    if dt < datetime.now(timezone.utc) - timedelta(seconds=60):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduled time must be in the future",
        )
    return dt


def _dedupe_clip_ids(clip_ids: list[int]) -> list[int]:
    """Deduplicate clip IDs while preserving request order."""
    return list(dict.fromkeys(clip_ids))


def _get_google_drive(request: Request, settings: Settings) -> Any:
    drive = getattr(request.app.state, "google_drive", None)
    if drive is None:
        from app.services.google_drive import GoogleDriveService

        drive = GoogleDriveService(settings)
        request.app.state.google_drive = drive
    return drive


def _get_youtube_publisher(request: Request, settings: Settings) -> YouTubePublisherService:
    publisher = getattr(request.app.state, "youtube_publisher", None)
    if publisher is None:
        publisher = YouTubePublisherService(settings)
        request.app.state.youtube_publisher = publisher
    return publisher


def _resolve_draft_file(raw_path: str | None, settings: Settings) -> Path | None:
    """Resolve a clip artifact path and verify it stays inside settings.drafts_dir."""
    if not raw_path or not str(raw_path).strip():
        return None
    drafts_root = Path(settings.drafts_dir).resolve()
    resolved = Path(str(raw_path)).resolve()
    if not resolved.is_relative_to(drafts_root):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Clip file path escapes drafts directory",
        )
    return resolved


def _serialize_clip(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data["id"],
        "streamer_id": data["streamer_id"],
        "streamer_name": data.get("streamer_name"),
        "session_id": data["session_id"],
        "chunk_id": data["chunk_id"],
        "draft_number": data["draft_number"],
        "title": data["title"],
        "description": data["description"],
        "tags": json.loads(data["tags_json"]),
        "file_path": data["file_path"],
        "thumbnail_path": data["thumbnail_path"],
        "drive_file_id": data["drive_file_id"],
        "drive_url": data["drive_url"],
        "tg_chat_id": data.get("tg_chat_id"),
        "tg_message_id": data.get("tg_message_id"),
        "start_sec": data["start_sec"],
        "end_sec": data["end_sec"],
        "duration_sec": data["duration_sec"],
        "score": data["score"],
        "status": data["status"],
        "scheduled_publish_at": data.get("scheduled_publish_at"),
        "scheduled_privacy_status": data.get("scheduled_privacy_status"),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


async def _fetch_clip_row(db: aiosqlite.Connection, clip_id: int) -> aiosqlite.Row | None:
    async with db.execute(
        """
        SELECT cd.*, s.name AS streamer_name
        FROM clip_drafts cd
        LEFT JOIN streamers s ON s.id = cd.streamer_id
        WHERE cd.id = ?
        """,
        (clip_id,),
    ) as cur:
        return await cur.fetchone()


async def _delete_single_clip(
    db: aiosqlite.Connection,
    clip: aiosqlite.Row,
    request: Request,
    settings: Settings,
    *,
    delete_drive: bool,
    drive_http_client: httpx.AsyncClient | None = None,
) -> tuple[bool, bool]:
    """Delete local draft artifacts, optional Drive file, and DB record for one clip."""
    deleted_local = False
    path = _resolve_draft_file(clip["file_path"], settings)
    if path is not None:
        if path.is_file():
            path.unlink()
            deleted_local = True
        srt_path = _resolve_draft_file(str(path.with_suffix(".srt")), settings)
        if srt_path is not None and srt_path.is_file():
            srt_path.unlink()
            deleted_local = True

    thumb = _resolve_draft_file(clip["thumbnail_path"], settings)
    if thumb is not None and thumb.is_file():
        thumb.unlink()
        deleted_local = True

    deleted_drive = False
    if delete_drive and clip["drive_file_id"]:
        drive = _get_google_drive(request, settings)
        try:
            delete_fn = drive.delete_file
            params = inspect.signature(delete_fn).parameters
            if drive_http_client is not None and "http_client" in params:
                deleted_drive = bool(
                    await delete_fn(
                        db,
                        str(clip["drive_file_id"]),
                        http_client=drive_http_client,
                    )
                )
            else:
                deleted_drive = bool(await delete_fn(db, str(clip["drive_file_id"])))
        except Exception as exc:
            logger.warning("Failed to delete Drive file %s: %s", clip["drive_file_id"], exc)
            deleted_drive = False

    await db.execute("DELETE FROM clip_drafts WHERE id = ?", (clip["id"],))
    await db.commit()
    return deleted_local, deleted_drive


@router.get("")
async def list_clips(
    status_filter: str | None = Query(default=None, alias="status"),
    streamer_id: int | None = Query(default=None),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """List clip drafts with optional status and streamer_id filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if status_filter is not None:
        clauses.append("cd.status = ?")
        params.append(status_filter)
    if streamer_id is not None:
        clauses.append("cd.streamer_id = ?")
        params.append(streamer_id)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = (
        f"SELECT cd.*, s.name AS streamer_name "
        f"FROM clip_drafts cd "
        f"LEFT JOIN streamers s ON s.id = cd.streamer_id "
        f"{where_sql} ORDER BY cd.id DESC"
    )
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return {"items": [_serialize_clip(row) for row in rows]}


@router.post("/bulk/approve")
async def bulk_approve_clips(
    payload: BulkApproveClipsRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Approve and sequentially publish multiple clip drafts, tolerating partial failures."""
    publisher = _get_youtube_publisher(request, settings)
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for cid in _dedupe_clip_ids(payload.clip_ids):
        row = await _fetch_clip_row(db, cid)
        if row is None:
            failed.append({"clip_id": cid, "detail": "Clip not found"})
            continue
        if row["status"] == "deleted":
            failed.append({"clip_id": cid, "detail": "Clip has been deleted"})
            continue

        try:
            await db.execute(
                """
                UPDATE clip_drafts
                SET status = 'approved',
                    scheduled_publish_at = NULL,
                    scheduled_privacy_status = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (utcnow_iso(), cid),
            )
            await db.commit()
            res = await _publish_clip_and_serialize(
                db,
                publisher,
                cid,
                publish_now=payload.publish_now,
                privacy_status=payload.privacy_status,
                youtube_account_ids=payload.youtube_account_ids,
            )
            succeeded.append(res)
        except HTTPException as exc:
            failed.append({"clip_id": cid, "detail": str(exc.detail)})
        except Exception as exc:
            logger.warning("Bulk approve failed for clip %s: %s", cid, exc)
            failed.append({"clip_id": cid, "detail": str(exc)})

    return {"succeeded": succeeded, "failed": failed}


@router.post("/bulk/reject")
async def bulk_reject_clips(
    payload: BulkRejectClipsRequest,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Reject multiple clip drafts (setting status='kept_local'), skipping posted/deleted."""
    updated: list[dict[str, Any]] = []
    skipped: list[int] = []
    missing: list[int] = []
    to_update: list[int] = []

    for cid in _dedupe_clip_ids(payload.clip_ids):
        row = await _fetch_clip_row(db, cid)
        if row is None:
            missing.append(cid)
            continue
        if row["status"] in ("posted", "deleted"):
            skipped.append(cid)
            continue
        to_update.append(cid)

    if to_update:
        now = utcnow_iso()
        # Batch IN clauses below SQLite's bind parameter limit.
        for offset in range(0, len(to_update), 500):
            batch = to_update[offset : offset + 500]
            placeholders = ", ".join("?" for _ in batch)
            await db.execute(
                f"UPDATE clip_drafts SET status = 'kept_local', scheduled_publish_at = NULL, scheduled_privacy_status = NULL, updated_at = ? WHERE id IN ({placeholders})",
                [now, *batch],
            )
        await db.commit()
        for cid in to_update:
            row = await _fetch_clip_row(db, cid)
            if row is not None:
                updated.append(_serialize_clip(row))

    return {"updated": updated, "skipped": skipped, "missing": missing}


@router.post("/bulk/delete")
async def bulk_delete_clips(
    payload: BulkDeleteClipsRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Delete multiple clip drafts and optionally their local/Drive files."""
    deleted_ids: list[int] = []
    failed: list[dict[str, Any]] = []
    drive_failed_ids: list[int] = []
    drive_calls = 0

    async def _run_batch(drive_client: httpx.AsyncClient | None) -> None:
        nonlocal drive_calls
        for cid in _dedupe_clip_ids(payload.clip_ids):
            async with db.execute("SELECT * FROM clip_drafts WHERE id = ?", (cid,)) as cur:
                clip = await cur.fetchone()
            if clip is None:
                failed.append({"clip_id": cid, "detail": "Clip not found"})
                continue

            has_drive_file = bool(payload.delete_drive and clip["drive_file_id"])
            if has_drive_file and drive_calls > 0 and BULK_DRIVE_DELETE_INTERVAL_SEC > 0:
                await asyncio.sleep(BULK_DRIVE_DELETE_INTERVAL_SEC)

            try:
                _, deleted_drive = await _delete_single_clip(
                    db,
                    clip,
                    request,
                    settings,
                    delete_drive=payload.delete_drive,
                    drive_http_client=drive_client,
                )
                if has_drive_file:
                    drive_calls += 1
                    if not deleted_drive:
                        drive_failed_ids.append(cid)
                deleted_ids.append(cid)
            except HTTPException as exc:
                failed.append({"clip_id": cid, "detail": str(exc.detail)})
            except Exception as exc:
                logger.warning("Bulk delete failed for clip %s: %s", cid, exc)
                failed.append({"clip_id": cid, "detail": str(exc)})

    if payload.delete_drive:
        async with httpx.AsyncClient(timeout=60.0) as drive_client:
            await _run_batch(drive_client)
    else:
        await _run_batch(None)

    return {
        "deleted_ids": deleted_ids,
        "failed": failed,
        "drive_failed_ids": drive_failed_ids,
    }


@router.post("/bulk/schedule")
async def bulk_schedule_clips(
    payload: BulkScheduleClipsRequest,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Schedule multiple clip drafts at a chosen time, optionally staggered by interval_minutes."""
    base_dt = _parse_future_schedule_utc(payload.scheduled_at)
    updated: list[dict[str, Any]] = []
    skipped: list[int] = []
    missing: list[int] = []

    slot = 0
    now = utcnow_iso()
    for cid in _dedupe_clip_ids(payload.clip_ids):
        row = await _fetch_clip_row(db, cid)
        if row is None:
            missing.append(cid)
            continue
        if row["status"] in ("posted", "deleted"):
            skipped.append(cid)
            continue

        target_dt = base_dt + timedelta(minutes=payload.interval_minutes * slot)
        slot += 1
        await db.execute(
            """
            UPDATE clip_drafts
            SET status = 'scheduled',
                scheduled_publish_at = ?,
                scheduled_privacy_status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (target_dt.isoformat(), payload.privacy_status, now, cid),
        )
        refreshed = await _fetch_clip_row(db, cid)
        if refreshed is not None:
            updated.append(_serialize_clip(refreshed))

    await db.commit()
    return {"updated": updated, "skipped": skipped, "missing": missing}


@router.get("/{clip_id}")
async def get_clip(
    clip_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Return a single clip draft by ID."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return _serialize_clip(row)


@router.patch("/{clip_id}")
async def update_clip(
    clip_id: int,
    payload: UpdateClipRequest,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Update editable fields (title, description, tags, status) on a clip draft."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    updates: list[str] = []
    params: list[Any] = []
    if payload.title is not None:
        updates.append("title = ?")
        params.append(payload.title)
    if payload.description is not None:
        updates.append("description = ?")
        params.append(payload.description)
    if payload.tags is not None:
        updates.append("tags_json = ?")
        params.append(json.dumps(payload.tags))
    if payload.status is not None:
        updates.append("status = ?")
        params.append(payload.status)

    updates.append("updated_at = ?")
    params.append(utcnow_iso())
    params.append(clip_id)

    await db.execute(
        f"UPDATE clip_drafts SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    await db.commit()

    updated = await _fetch_clip_row(db, clip_id)
    assert updated is not None
    return _serialize_clip(updated)


@router.get("/{clip_id}/video")
async def get_clip_video(
    clip_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Stream/serve the exported MP4 clip file."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    file_path = _resolve_draft_file(row["file_path"], settings)
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip video file not found")
    return FileResponse(path=str(file_path), media_type="video/mp4", filename=file_path.name)


@router.get("/{clip_id}/thumbnail")
async def get_clip_thumbnail(
    clip_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Serve the extracted JPEG cover thumbnail."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    thumb_path = _resolve_draft_file(row["thumbnail_path"], settings)
    if thumb_path is None or not thumb_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip thumbnail not found")
    return FileResponse(path=str(thumb_path), media_type="image/jpeg", filename=thumb_path.name)


@router.post("/{clip_id}/upload-drive")
async def upload_clip_to_drive(
    clip_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Upload a local clip MP4 to Google Drive and store its shareable link."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    file_path = _resolve_draft_file(row["file_path"], settings)
    if file_path is None or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip video file not found")

    drive = _get_google_drive(request, settings)
    streamer_name = str(row["streamer_name"] or f"streamer-{row['streamer_id']}")
    clip_date = str(row["created_at"] or utcnow_iso())[:10]

    try:
        upload_res = await drive.upload_clip(
            db,
            file_path,
            streamer_name=streamer_name,
            clip_date=clip_date,
            display_name=file_path.name,
            share_anyone=True,
        )
    except Exception as exc:
        logger.warning("Google Drive upload failed for clip %s: %s", clip_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google Drive upload failed: {exc}",
        ) from exc

    if not upload_res:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Google Drive account connected",
        )

    await db.execute(
        """
        UPDATE clip_drafts
        SET drive_file_id = ?, drive_url = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            upload_res.get("drive_file_id"),
            upload_res.get("drive_url"),
            utcnow_iso(),
            clip_id,
        ),
    )
    await db.commit()

    updated = await _fetch_clip_row(db, clip_id)
    assert updated is not None
    return _serialize_clip(updated)


async def _publish_clip_and_serialize(
    db: aiosqlite.Connection,
    publisher: YouTubePublisherService,
    clip_id: int,
    *,
    publish_now: bool = True,
    privacy_status: PrivacyStatus | None = None,
    youtube_account_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Optionally publish a clip draft and return the serialized clip with publications."""
    if publish_now:
        try:
            await publisher.publish_clip(
                db,
                clip_id,
                privacy_override=privacy_status,
                account_ids=youtube_account_ids,
            )
        except (YouTubePublisherError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except (FileNotFoundError, LookupError) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc

    updated = await _fetch_clip_row(db, clip_id)
    assert updated is not None
    publications = await publisher.list_publications(db, clip_id)
    return {
        "clip": _serialize_clip(updated),
        "publications": publications,
    }


@router.post("/{clip_id}/approve")
async def approve_clip(
    clip_id: int,
    request: Request,
    payload: ApproveClipRequest | None = Body(default=None),
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Approve a clip draft and optionally publish it to configured YouTube targets."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    await db.execute(
        """
        UPDATE clip_drafts
        SET status = 'approved',
            scheduled_publish_at = NULL,
            scheduled_privacy_status = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (utcnow_iso(), clip_id),
    )
    await db.commit()

    publisher = _get_youtube_publisher(request, settings)
    return await _publish_clip_and_serialize(
        db,
        publisher,
        clip_id,
        publish_now=True if payload is None else payload.publish_now,
        privacy_status=payload.privacy_status if payload else None,
        youtube_account_ids=payload.youtube_account_ids if payload else None,
    )


@router.post("/{clip_id}/reject")
async def reject_clip(
    clip_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Reject a clip draft for publishing and keep it locally as kept_local."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    await db.execute(
        """
        UPDATE clip_drafts
        SET status = 'kept_local',
            scheduled_publish_at = NULL,
            scheduled_privacy_status = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (utcnow_iso(), clip_id),
    )
    await db.commit()

    updated = await _fetch_clip_row(db, clip_id)
    assert updated is not None
    return _serialize_clip(updated)


@router.post("/{clip_id}/schedule")
async def schedule_clip(
    clip_id: int,
    payload: ScheduleClipRequest,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Schedule a single clip draft to be published automatically at scheduled_at."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    if row["status"] in ("posted", "deleted"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot schedule a clip with status '{row['status']}'",
        )

    target_dt = _parse_future_schedule_utc(payload.scheduled_at)
    await db.execute(
        """
        UPDATE clip_drafts
        SET status = 'scheduled',
            scheduled_publish_at = ?,
            scheduled_privacy_status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (target_dt.isoformat(), payload.privacy_status, utcnow_iso(), clip_id),
    )
    await db.commit()

    updated = await _fetch_clip_row(db, clip_id)
    assert updated is not None
    return _serialize_clip(updated)


@router.delete("/{clip_id}/schedule")
async def cancel_clip_schedule(
    clip_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Cancel a pending scheduled publish and return the clip to draft status."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    next_status = "draft" if row["status"] == "scheduled" else row["status"]
    await db.execute(
        """
        UPDATE clip_drafts
        SET status = ?,
            scheduled_publish_at = NULL,
            scheduled_privacy_status = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (next_status, utcnow_iso(), clip_id),
    )
    await db.commit()

    updated = await _fetch_clip_row(db, clip_id)
    assert updated is not None
    return _serialize_clip(updated)


@router.post("/{clip_id}/publish")
async def publish_clip(
    clip_id: int,
    request: Request,
    payload: PublishClipRequest | None = Body(default=None),
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Publish a clip draft directly to mapped YouTube channels."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    publisher = _get_youtube_publisher(request, settings)
    return await _publish_clip_and_serialize(
        db,
        publisher,
        clip_id,
        publish_now=True,
        privacy_status=payload.privacy_status if payload else None,
        youtube_account_ids=payload.youtube_account_ids if payload else None,
    )


@router.get("/{clip_id}/publications")
async def list_clip_publications(
    clip_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List YouTube publication attempts/results for a clip draft."""
    row = await _fetch_clip_row(db, clip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    publisher = _get_youtube_publisher(request, settings)
    items = await publisher.list_publications(db, clip_id)
    return {"clip_id": clip_id, "items": items}


@router.delete("/{clip_id}")
async def delete_clip(
    clip_id: int,
    request: Request,
    delete_drive: bool = Query(default=False),
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Delete a clip draft record and optionally its local/Drive files."""
    async with db.execute("SELECT * FROM clip_drafts WHERE id = ?", (clip_id,)) as cur:
        clip = await cur.fetchone()
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")

    deleted_local, deleted_drive = await _delete_single_clip(
        db,
        clip,
        request,
        settings,
        delete_drive=delete_drive,
    )
    return {
        "ok": True,
        "deleted_local": deleted_local,
        "deleted_drive": deleted_drive,
    }
