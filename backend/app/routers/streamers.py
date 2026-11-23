from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import get_db, utcnow_iso
from app.services.recorder import capture_live_preview_frame, capture_preview_frame
from app.services.youtube_watcher import YouTubeWatcher
from app.storage import slugify_streamer, streamer_preview_path

router = APIRouter(prefix="/api/streamers", tags=["streamers"])

DEFAULT_CLIP_OPTS: dict[str, Any] = {
    "min_len": 30,
    "max_len": 60,
    "cam_auto": True,
    "layout": "stack",
    "fallback_layout": "blur",
    "caption_style": "hormozi",
    "language": None,
    "clip_count": 3,
    "hashtags": [],
}

MAX_CLIPS_PER_CHUNK = 10
MAX_HASHTAGS = 15
VALID_LAYOUTS = {
    "stack",
    "bottom",
    "blur",
    "fill",
    "game",
    "pip_tl",
    "pip_tr",
    "pip_bl",
    "pip_br",
    "circle",
    "side",
}


def normalize_cam_box(raw: Any) -> dict[str, float] | None:
    """Validate and normalize a custom webcam crop box {x, y, w, h} in [0..1]."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Custom webcam frame must be an object with x, y, w, h")
    try:
        vals = {k: float(raw[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Custom webcam frame requires numeric x, y, w, h")
    if not all(math.isfinite(v) for v in vals.values()):
        raise HTTPException(status_code=422, detail="Custom webcam frame coordinates must be finite numbers")
    x = max(0.0, min(0.95, vals["x"]))
    y = max(0.0, min(0.95, vals["y"]))
    w = max(0.05, min(1.0 - x, vals["w"]))
    h = max(0.05, min(1.0 - y, vals["h"]))
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def normalize_hashtags(raw: Any) -> list[str]:
    """Accept a list or a space/comma separated string; return unique #tags without spaces."""
    items = raw if isinstance(raw, list) else str(raw or "").replace(",", " ").split()
    tags: list[str] = []
    for item in items:
        word = "".join(ch for ch in str(item).strip().lstrip("#") if ch.isalnum() or ch == "_")
        if word and f"#{word}".lower() not in {t.lower() for t in tags}:
            tags.append(f"#{word}")
    return tags[:MAX_HASHTAGS]


def clean_clip_opts(opts: dict[str, Any]) -> dict[str, Any]:
    """Validate the fields the clip processor and publisher read from clip_opts."""
    out = dict(opts)
    if "clip_count" in out:
        try:
            out["clip_count"] = max(1, min(MAX_CLIPS_PER_CHUNK, int(out["clip_count"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Clips per chunk must be a whole number from 1 to 10")
    if "hashtags" in out:
        out["hashtags"] = normalize_hashtags(out["hashtags"])
    for key in ("min_len", "max_len"):
        if key in out:
            try:
                out[key] = max(5, min(300, int(out[key])))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="Clip lengths must be whole seconds")
    if out.get("min_len") and out.get("max_len") and out["min_len"] > out["max_len"]:
        raise HTTPException(status_code=422, detail="The shortest clip cannot be longer than the longest")
    if "cam_box" in out:
        out["cam_box"] = normalize_cam_box(out["cam_box"])
    if "layout" in out and out["layout"] is not None:
        layout = str(out["layout"]).strip().lower()
        if layout not in VALID_LAYOUTS:
            raise HTTPException(status_code=422, detail=f"Unsupported layout '{layout}'")
        out["layout"] = layout
    return out


class StreamerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    platform: Literal["youtube", "twitch"]
    channel_url: str = Field(min_length=1, max_length=500)
    auto_watch: bool = True
    clip_opts: dict[str, Any] | None = None


class StreamerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    platform: Literal["youtube", "twitch"] | None = None
    channel_url: str | None = Field(default=None, min_length=1, max_length=500)
    auto_watch: bool | None = None
    clip_opts: dict[str, Any] | None = None


def derive_channel_ref(platform: str, channel_url: str) -> str:
    """Extract a canonical channel handle, login, or ID from a channel URL."""
    parsed = urlparse(channel_url.strip())
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return channel_url.strip()
    if platform == "twitch":
        return parts[0].lower()
    # YouTube URLs can be /@handle, /channel/UC..., /c/name, /user/name
    if parts[0] in {"channel", "c", "user"} and len(parts) > 1:
        return parts[1]
    return parts[0]


def _serialize_streamer(row: aiosqlite.Row, settings: Settings | None = None) -> dict[str, Any]:
    preview_file = streamer_preview_path(settings, int(row["id"]))
    has_preview = preview_file.is_file() and preview_file.stat().st_size > 0
    preview_updated_at = (
        datetime.fromtimestamp(preview_file.stat().st_mtime, tz=timezone.utc).isoformat()
        if has_preview
        else None
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "platform": row["platform"],
        "channel_url": row["channel_url"],
        "channel_ref": row["channel_ref"],
        "auto_watch": bool(row["auto_watch"]),
        "clip_opts": json.loads(row["clip_opts_json"]),
        "is_live": bool(row["is_live"]),
        "recording_held": bool(row["hold_until_offline"]) if "hold_until_offline" in row.keys() else False,
        "has_preview": has_preview,
        "preview_updated_at": preview_updated_at,
        "last_checked_at": row["last_checked_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("")
async def list_streamers(
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """List all configured streamers."""
    async with db.execute("SELECT * FROM streamers ORDER BY id ASC") as cur:
        rows = await cur.fetchall()
    return {"items": [_serialize_streamer(row, settings) for row in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_streamer(
    payload: StreamerCreate,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Create a new streamer record with derived slug, channel_ref, and default clip_opts."""
    slug = slugify_streamer(payload.name)
    channel_ref = derive_channel_ref(payload.platform, payload.channel_url)
    merged_opts = clean_clip_opts({**DEFAULT_CLIP_OPTS, **(payload.clip_opts or {})})
    now = utcnow_iso()

    try:
        cursor = await db.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                payload.name.strip(),
                slug,
                payload.platform,
                payload.channel_url.strip(),
                channel_ref,
                1 if payload.auto_watch else 0,
                json.dumps(merged_opts),
                now,
                now,
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Streamer with slug '{slug}' already exists",
        ) from exc

    async with db.execute("SELECT * FROM streamers WHERE id = ?", (cursor.lastrowid,)) as cur:
        row = await cur.fetchone()
    return _serialize_streamer(row, settings)


@router.patch("/{streamer_id}")
async def update_streamer(
    streamer_id: int,
    payload: StreamerUpdate,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Partially update an existing streamer."""
    async with db.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")

    name = payload.name.strip() if payload.name is not None else existing["name"]
    slug = slugify_streamer(name) if payload.name is not None else existing["slug"]
    platform = payload.platform if payload.platform is not None else existing["platform"]
    channel_url = (
        payload.channel_url.strip() if payload.channel_url is not None else existing["channel_url"]
    )
    channel_ref = (
        derive_channel_ref(platform, channel_url)
        if (payload.channel_url is not None or payload.platform is not None)
        else existing["channel_ref"]
    )
    auto_watch = (
        (1 if payload.auto_watch else 0)
        if payload.auto_watch is not None
        else existing["auto_watch"]
    )
    existing_opts = json.loads(existing["clip_opts_json"])
    clip_opts = (
        clean_clip_opts({**existing_opts, **payload.clip_opts})
        if payload.clip_opts is not None
        else existing_opts
    )
    updated_at = utcnow_iso()

    try:
        await db.execute(
            """
            UPDATE streamers
            SET name = ?, slug = ?, platform = ?, channel_url = ?, channel_ref = ?,
                auto_watch = ?, clip_opts_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name,
                slug,
                platform,
                channel_url,
                channel_ref,
                auto_watch,
                json.dumps(clip_opts),
                updated_at,
                streamer_id,
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Streamer with slug '{slug}' already exists",
        ) from exc

    async with db.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        updated = await cur.fetchone()
    return _serialize_streamer(updated, settings)


@router.get("/{streamer_id}/preview")
async def get_streamer_preview(
    streamer_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """Serve the latest captured 16:9 live reference screenshot for a streamer."""
    async with db.execute("SELECT id FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")
    preview = streamer_preview_path(settings, streamer_id)
    if not preview.is_file() or preview.stat().st_size == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No live screenshot captured for this streamer yet",
        )
    return FileResponse(
        path=str(preview),
        media_type="image/jpeg",
        filename=preview.name,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{streamer_id}/preview/refresh")
async def refresh_streamer_preview(
    streamer_id: int,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Capture a fresh 16:9 reference frame from an active recording, recent chunk, or live stream."""
    async with db.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")

    dest = streamer_preview_path(settings, streamer_id)
    captured = False

    recorder = getattr(request.app.state, "recorder", None)
    if recorder is not None:
        seg = recorder.active_segment_path(streamer_id)
        if seg is not None and seg.is_file():
            captured = await capture_preview_frame(seg, dest)

    if not captured:
        async with db.execute(
            """
            SELECT file_path FROM recording_chunks
            WHERE streamer_id = ? AND status != 'deleted'
            ORDER BY id DESC LIMIT 5
            """,
            (streamer_id,),
        ) as cur:
            chunk_rows = await cur.fetchall()
        for cr in chunk_rows:
            fp = Path(str(cr["file_path"] or ""))
            if fp.is_file():
                captured = await capture_preview_frame(fp, dest)
                if captured:
                    break

    if not captured:
        platform = str(row["platform"])
        stream_url = (
            YouTubeWatcher.build_live_url(str(row["channel_ref"]))
            if platform == "youtube"
            else str(row["channel_url"])
        )
        captured = await capture_live_preview_frame(
            stream_url,
            dest,
            platform=platform,
            cookies_file=getattr(settings, "youtube_cookies_file", ""),
        )

    if not captured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not capture a screenshot yet. Wait until the streamer is live or has a recording on disk.",
        )

    return _serialize_streamer(row, settings)


@router.delete("/{streamer_id}")
async def delete_streamer(
    streamer_id: int,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Delete a streamer by ID."""
    async with db.execute("SELECT id FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")

    await db.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))
    await db.commit()
    streamer_preview_path(settings, streamer_id).unlink(missing_ok=True)
    return {"ok": True, "id": streamer_id}
