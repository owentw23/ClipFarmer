from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.config import Settings, get_settings
from app.db import get_db
from app.services.metrics_collector import MetricsCollectorService
from app.services.metrics_dashboard import build_metrics_dashboard

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/summary")
async def get_metrics_summary(db: aiosqlite.Connection = Depends(get_db)) -> dict[str, Any]:
    """Return aggregate clip, publication, and view/watch metrics."""
    async with db.execute("SELECT COUNT(*) AS cnt FROM clip_drafts") as cur:
        row = await cur.fetchone()
        total_clips = int(row["cnt"]) if row else 0

    async with db.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM publications
        WHERE status = 'published' OR youtube_video_id IS NOT NULL
        """
    ) as cur:
        row = await cur.fetchone()
        total_posted = int(row["cnt"]) if row else 0

    async with db.execute(
        """
        SELECT
            p.id AS publication_id,
            p.clip_draft_id,
            p.youtube_video_id,
            p.youtube_url,
            p.status AS publication_status,
            p.published_at,
            cd.title AS clip_title,
            cd.streamer_id,
            COALESCE(ms.views, 0) AS views,
            COALESCE(ms.likes, 0) AS likes,
            COALESCE(ms.comments, 0) AS comments,
            COALESCE(ms.estimated_minutes_watched, 0.0) AS estimated_minutes_watched,
            ms.captured_at
        FROM publications p
        LEFT JOIN clip_drafts cd ON cd.id = p.clip_draft_id
        LEFT JOIN metric_snapshots ms ON ms.id = (
            SELECT ms2.id
            FROM metric_snapshots ms2
            WHERE ms2.publication_id = p.id
            ORDER BY ms2.captured_at DESC, ms2.id DESC
            LIMIT 1
        )
        ORDER BY p.id DESC
        """
    ) as cur:
        rows = await cur.fetchall()

    items = [dict(r) for r in rows]
    total_views = sum(int(item["views"]) for item in items)
    total_likes = sum(int(item["likes"]) for item in items)
    total_watch_minutes = round(
        sum(float(item["estimated_minutes_watched"]) for item in items), 2
    )

    return {
        "total_clips": total_clips,
        "total_posted": total_posted,
        "total_views": total_views,
        "total_likes": total_likes,
        "total_watch_minutes": total_watch_minutes,
        "items": items,
    }


@router.get("/dashboard")
async def get_metrics_dashboard(
    days: Literal["7", "30", "all"] = Query(default="7"),
    streamer_id: int | None = Query(default=None),
    youtube_account_id: int | None = Query(default=None),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Return full Phase 5 dashboard payload: KPIs, Best Clip This Week, charts, and clips."""
    try:
        return await build_metrics_dashboard(
            db,
            window_raw=days,
            streamer_id=streamer_id,
            youtube_account_id=youtube_account_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/sync")
async def sync_metrics_now(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Trigger immediate YouTube Data API v3 + Analytics API v2 snapshot sync."""
    collector = getattr(request.app.state, "metrics_collector", None)
    if collector is None:
        collector = MetricsCollectorService(settings)
        request.app.state.metrics_collector = collector
    return await collector.sync_all(db=db)
