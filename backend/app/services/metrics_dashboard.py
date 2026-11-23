from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


WINDOW_DAYS: dict[str, int | None] = {
    "7": 7,
    "7d": 7,
    "30": 30,
    "30d": 30,
    "all": None,
}

COHORT_ORDER = ("<30s", "30-45s", "45-60s", "60s+")


def normalize_window(raw: str | None) -> tuple[str, int | None]:
    key = (raw or "7").strip().lower()
    if key not in WINDOW_DAYS:
        raise ValueError(f"Unsupported days window: {raw!r}. Use 7, 30, or all.")
    days = WINDOW_DAYS[key]
    label = f"{days}d" if days is not None else "all"
    return label, days


def _duration_bucket(duration_sec: float) -> str:
    if duration_sec < 30:
        return "<30s"
    if duration_sec < 45:
        return "30-45s"
    if duration_sec < 60:
        return "45-60s"
    return "60s+"


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _compute_pub_metrics(
    pub: dict[str, Any],
    snaps: list[dict[str, Any]],
    now_utc: datetime,
) -> dict[str, Any]:
    latest = snaps[-1] if snaps else {}
    views = int(latest.get("views") or 0)
    likes = int(latest.get("likes") or 0)
    comments = int(latest.get("comments") or 0)
    shares = int(latest.get("shares") or 0)
    engaged_views = int(latest.get("engaged_views") or 0)
    estimated_minutes_watched = float(latest.get("estimated_minutes_watched") or 0.0)
    subscribers_gained = int(latest.get("subscribers_gained") or 0)
    subscribers_lost = int(latest.get("subscribers_lost") or 0)
    avg_view_duration_sec = float(latest.get("avg_view_duration_sec") or 0.0)
    avg_view_percentage = float(latest.get("avg_view_percentage") or 0.0)
    shorts_feed_views = int(latest.get("shorts_feed_views") or 0)
    analytics_synced_at = latest.get("analytics_synced_at")
    captured_at = latest.get("captured_at")

    views_delta_24h = 0
    if len(snaps) > 1:
        cutoff_24h = now_utc - timedelta(hours=24)
        baseline = None
        for snap in snaps[:-1]:
            snap_dt = _parse_iso_dt(snap.get("captured_at"))
            if snap_dt is not None and snap_dt <= cutoff_24h:
                baseline = snap
        if baseline is None:
            baseline = snaps[0]
        views_delta_24h = max(0, views - int(baseline.get("views") or 0))

    raw_path = pub.get("file_path")
    has_local_video = bool(raw_path and Path(str(raw_path)).is_file())
    engagement_rate = (
        round((likes + comments + shares) / views * 100.0, 2) if views > 0 else 0.0
    )

    return {
        "publication_id": int(pub["publication_id"]),
        "clip_draft_id": int(pub["clip_draft_id"]),
        "title": pub["title"],
        "streamer_id": int(pub["streamer_id"]),
        "streamer_name": pub.get("streamer_name") or f"Streamer {pub['streamer_id']}",
        "youtube_account_id": int(pub["youtube_account_id"]),
        "channel_title": pub.get("channel_title") or "YouTube Channel",
        "youtube_video_id": pub.get("youtube_video_id"),
        "youtube_url": pub.get("youtube_url"),
        "privacy_status": pub.get("privacy_status") or "public",
        "published_at": pub.get("published_at") or pub.get("pub_created_at"),
        "duration_sec": round(float(pub.get("duration_sec") or 0.0), 1),
        "score": round(float(pub.get("score") or 0.0), 3),
        "views": views,
        "views_delta_24h": views_delta_24h,
        "engaged_views": engaged_views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "engagement_rate": engagement_rate,
        "estimated_minutes_watched": round(estimated_minutes_watched, 2),
        "watch_hours": round(estimated_minutes_watched / 60.0, 2),
        "avg_view_duration_sec": round(avg_view_duration_sec, 1),
        "avg_view_percentage": round(avg_view_percentage, 1),
        "subscribers_gained": subscribers_gained,
        "subscribers_lost": subscribers_lost,
        "net_subscribers": subscribers_gained - subscribers_lost,
        "shorts_feed_views": shorts_feed_views,
        "analytics_pending": analytics_synced_at is None,
        "analytics_synced_at": analytics_synced_at,
        "captured_at": captured_at,
        "has_local_video": has_local_video,
    }


def _weighted_avg(items: list[dict[str, Any]], key: str) -> float:
    synced = [item for item in items if not item["analytics_pending"]]
    if not synced:
        return 0.0
    total_weight = sum(max(1, int(item["views"])) for item in synced)
    weighted = sum(float(item[key]) * max(1, int(item["views"])) for item in synced)
    return round(weighted / total_weight, 1) if total_weight > 0 else 0.0


async def build_metrics_dashboard(
    db: aiosqlite.Connection,
    *,
    window_raw: str | None = "7",
    streamer_id: int | None = None,
    youtube_account_id: int | None = None,
) -> dict[str, Any]:
    """Build aggregated Phase 5 metrics dashboard payload from SQLite snapshots."""
    window_label, days = normalize_window(window_raw)
    now_utc = datetime.now(timezone.utc)
    cutoff_iso = (
        (now_utc - timedelta(days=days)).isoformat() if days is not None else None
    )
    week_cutoff_iso = (now_utc - timedelta(days=7)).isoformat()

    async with db.execute("SELECT id, name FROM streamers ORDER BY name ASC") as cur:
        streamer_options = [
            {"id": int(r["id"]), "name": r["name"]} for r in await cur.fetchall()
        ]

    async with db.execute(
        """
        SELECT id, channel_id, channel_title
        FROM youtube_accounts
        WHERE status = 'active'
          AND COALESCE(oauth_provider, 'google') != 'google_drive'
          AND channel_id NOT LIKE 'drive-only:%'
        ORDER BY id ASC
        """
    ) as cur:
        account_options = [
            {
                "id": int(r["id"]),
                "channel_id": r["channel_id"],
                "channel_title": r["channel_title"] or r["channel_id"],
            }
            for r in await cur.fetchall()
        ]

    async with db.execute(
        "SELECT MAX(captured_at) AS last_synced_at FROM metric_snapshots"
    ) as cur:
        sync_row = await cur.fetchone()
        last_synced_at = sync_row["last_synced_at"] if sync_row else None

    base_clauses = ["p.status IN ('published', 'posted')"]
    base_params: list[Any] = []
    if streamer_id is not None:
        base_clauses.append("cd.streamer_id = ?")
        base_params.append(streamer_id)
    if youtube_account_id is not None:
        base_clauses.append("p.youtube_account_id = ?")
        base_params.append(youtube_account_id)

    window_clauses = list(base_clauses)
    window_params = list(base_params)
    if cutoff_iso is not None:
        window_clauses.append("COALESCE(p.published_at, p.created_at) >= ?")
        window_params.append(cutoff_iso)

    week_clauses = list(base_clauses) + ["COALESCE(p.published_at, p.created_at) >= ?"]
    week_params = list(base_params) + [week_cutoff_iso]

    select_sql = """
        SELECT p.id AS publication_id,
               p.clip_draft_id,
               p.youtube_account_id,
               p.youtube_video_id,
               p.youtube_url,
               p.privacy_status,
               p.published_at,
               p.created_at AS pub_created_at,
               cd.streamer_id,
               cd.title,
               cd.file_path,
               cd.duration_sec,
               cd.score,
               s.name AS streamer_name,
               ya.channel_title
        FROM publications p
        JOIN clip_drafts cd ON cd.id = p.clip_draft_id
        LEFT JOIN streamers s ON s.id = cd.streamer_id
        LEFT JOIN youtube_accounts ya ON ya.id = p.youtube_account_id
    """

    async with db.execute(
        f"{select_sql} WHERE {' AND '.join(window_clauses)} ORDER BY p.id DESC",
        window_params,
    ) as cur:
        window_pub_rows = [dict(r) for r in await cur.fetchall()]

    async with db.execute(
        f"{select_sql} WHERE {' AND '.join(week_clauses)} ORDER BY p.id DESC",
        week_params,
    ) as cur:
        week_pub_rows = [dict(r) for r in await cur.fetchall()]

    all_pub_ids = sorted(
        {
            *(int(r["publication_id"]) for r in window_pub_rows),
            *(int(r["publication_id"]) for r in week_pub_rows),
        }
    )

    snaps_by_pub: dict[int, list[dict[str, Any]]] = {}
    if all_pub_ids:
        placeholders = ",".join("?" for _ in all_pub_ids)
        async with db.execute(
            f"""
            SELECT publication_id, views, likes, comments,
                   estimated_minutes_watched, engaged_views, shares,
                   subscribers_gained, subscribers_lost,
                   avg_view_duration_sec, avg_view_percentage,
                   shorts_feed_views, analytics_synced_at, captured_at
            FROM metric_snapshots
            WHERE publication_id IN ({placeholders})
            ORDER BY captured_at ASC, id ASC
            """,
            all_pub_ids,
        ) as cur:
            for row in await cur.fetchall():
                d = dict(row)
                snaps_by_pub.setdefault(int(d["publication_id"]), []).append(d)

    window_clips = [
        _compute_pub_metrics(pub, snaps_by_pub.get(int(pub["publication_id"]), []), now_utc)
        for pub in window_pub_rows
    ]
    window_clips.sort(
        key=lambda c: (
            c["views"],
            c["engaged_views"],
            c["likes"],
            c["published_at"] or "",
            c["publication_id"],
        ),
        reverse=True,
    )

    week_clips = [
        _compute_pub_metrics(pub, snaps_by_pub.get(int(pub["publication_id"]), []), now_utc)
        for pub in week_pub_rows
    ]
    week_clips.sort(
        key=lambda c: (
            c["views"],
            c["engaged_views"],
            c["likes"],
            c["published_at"] or "",
            c["publication_id"],
        ),
        reverse=True,
    )
    best_clip_this_week = week_clips[0] if week_clips else None

    # Funnel metrics from clip_drafts scoped by window and streamer_id
    draft_clauses = ["COALESCE(cd.status, 'draft') != 'deleted'"]
    draft_params: list[Any] = []
    if cutoff_iso is not None:
        draft_clauses.append("cd.created_at >= ?")
        draft_params.append(cutoff_iso)
    if streamer_id is not None:
        draft_clauses.append("cd.streamer_id = ?")
        draft_params.append(streamer_id)

    async with db.execute(
        f"""
        SELECT id, status, tg_message_id
        FROM clip_drafts cd
        WHERE {' AND '.join(draft_clauses)}
        """,
        draft_params,
    ) as cur:
        draft_rows = [dict(r) for r in await cur.fetchall()]

    drafts_generated = len(draft_rows)
    notified_count = sum(
        1
        for r in draft_rows
        if r["status"] in {"notified", "approved", "posted"} or r["tg_message_id"] is not None
    )
    approved_count = sum(1 for r in draft_rows if r["status"] in {"approved", "posted"})
    distinct_posted_clips = len({c["clip_draft_id"] for c in window_clips})
    kept_local_count = sum(1 for r in draft_rows if r["status"] == "kept_local")
    archived_count = sum(1 for r in draft_rows if r["status"] == "archived")
    failed_count = sum(1 for r in draft_rows if r["status"] == "failed")

    total_views = sum(c["views"] for c in window_clips)
    total_engaged_views = sum(c["engaged_views"] for c in window_clips)
    total_likes = sum(c["likes"] for c in window_clips)
    total_comments = sum(c["comments"] for c in window_clips)
    total_shares = sum(c["shares"] for c in window_clips)
    total_watch_minutes = round(
        sum(c["estimated_minutes_watched"] for c in window_clips), 2
    )
    watch_hours = round(total_watch_minutes / 60.0, 2)
    subscribers_gained = sum(c["subscribers_gained"] for c in window_clips)
    subscribers_lost = sum(c["subscribers_lost"] for c in window_clips)
    total_shorts_feed_views = sum(c["shorts_feed_views"] for c in window_clips)

    engagement_rate = (
        round((total_likes + total_comments + total_shares) / total_views * 100.0, 2)
        if total_views > 0
        else 0.0
    )
    shorts_feed_share = (
        round(total_shorts_feed_views / total_views * 100.0, 1)
        if total_views > 0
        else 0.0
    )
    approval_rate = (
        round(approved_count / drafts_generated * 100.0, 1)
        if drafts_generated > 0
        else 0.0
    )

    # Daily growth timeseries using cumulative latest snapshot per publication up to each date
    window_pub_ids = [int(r["publication_id"]) for r in window_pub_rows]
    cutoff_date_str = cutoff_iso[:10] if cutoff_iso is not None else None
    all_dates = sorted(
        {
            str(snap["captured_at"])[:10]
            for pid in window_pub_ids
            for snap in snaps_by_pub.get(pid, [])
            if snap.get("captured_at")
        }
    )
    if cutoff_date_str is not None:
        all_dates = [d for d in all_dates if d >= cutoff_date_str]

    timeseries: list[dict[str, Any]] = []
    for day_str in all_dates:
        day_views = 0
        day_engaged = 0
        day_likes = 0
        day_watch_min = 0.0
        day_subs = 0
        for pid in window_pub_ids:
            latest_on_day = None
            for snap in snaps_by_pub.get(pid, []):
                if str(snap.get("captured_at") or "")[:10] <= day_str:
                    latest_on_day = snap
            if latest_on_day is not None:
                day_views += int(latest_on_day.get("views") or 0)
                day_engaged += int(latest_on_day.get("engaged_views") or 0)
                day_likes += int(latest_on_day.get("likes") or 0)
                day_watch_min += float(
                    latest_on_day.get("estimated_minutes_watched") or 0.0
                )
                day_subs += int(latest_on_day.get("subscribers_gained") or 0) - int(
                    latest_on_day.get("subscribers_lost") or 0
                )
        timeseries.append(
            {
                "date": day_str,
                "views": day_views,
                "engaged_views": day_engaged,
                "likes": day_likes,
                "watch_hours": round(day_watch_min / 60.0, 2),
                "net_subscribers": day_subs,
            }
        )

    # Duration cohorts
    cohort_buckets: dict[str, list[dict[str, Any]]] = {b: [] for b in COHORT_ORDER}
    for clip in window_clips:
        cohort_buckets[_duration_bucket(clip["duration_sec"])].append(clip)

    cohorts = []
    for bucket in COHORT_ORDER:
        items_in_bucket = cohort_buckets[bucket]
        b_views = sum(c["views"] for c in items_in_bucket)
        b_count = len(items_in_bucket)
        cohorts.append(
            {
                "bucket": bucket,
                "clip_count": b_count,
                "total_views": b_views,
                "avg_views": round(b_views / b_count, 1) if b_count > 0 else 0.0,
                "avg_view_percentage": _weighted_avg(
                    items_in_bucket, "avg_view_percentage"
                ),
            }
        )

    # Per-streamer breakdown
    by_streamer: dict[int, list[dict[str, Any]]] = {}
    for clip in window_clips:
        by_streamer.setdefault(clip["streamer_id"], []).append(clip)

    streamers_breakdown = []
    for sid, s_clips in by_streamer.items():
        s_watch_min = sum(c["estimated_minutes_watched"] for c in s_clips)
        streamers_breakdown.append(
            {
                "streamer_id": sid,
                "streamer_name": s_clips[0]["streamer_name"],
                "clips_posted": len(s_clips),
                "views": sum(c["views"] for c in s_clips),
                "engaged_views": sum(c["engaged_views"] for c in s_clips),
                "watch_hours": round(s_watch_min / 60.0, 2),
                "avg_view_percentage": _weighted_avg(s_clips, "avg_view_percentage"),
                "net_subscribers": sum(c["net_subscribers"] for c in s_clips),
            }
        )
    streamers_breakdown.sort(
        key=lambda item: (item["views"], item["streamer_name"]), reverse=True
    )

    return {
        "filters": {
            "window": window_label,
            "streamer_id": streamer_id,
            "youtube_account_id": youtube_account_id,
            "streamers": streamer_options,
            "accounts": account_options,
            "last_synced_at": last_synced_at,
        },
        "kpis": {
            "total_views": total_views,
            "total_engaged_views": total_engaged_views,
            "engagement_rate": engagement_rate,
            "watch_hours": watch_hours,
            "total_watch_minutes": total_watch_minutes,
            "avg_view_percentage": _weighted_avg(window_clips, "avg_view_percentage"),
            "avg_view_duration_sec": _weighted_avg(
                window_clips, "avg_view_duration_sec"
            ),
            "net_subscribers": subscribers_gained - subscribers_lost,
            "subscribers_gained": subscribers_gained,
            "subscribers_lost": subscribers_lost,
            "total_likes": total_likes,
            "total_comments": total_comments,
            "total_shares": total_shares,
            "total_shorts_feed_views": total_shorts_feed_views,
            "shorts_feed_share": shorts_feed_share,
            "posted_count": len(window_clips),
            "drafts_generated": drafts_generated,
            "approval_rate": approval_rate,
        },
        "best_clip_this_week": best_clip_this_week,
        "timeseries": timeseries,
        "funnel": {
            "stages": [
                {"stage": "Generated", "key": "generated", "count": drafts_generated},
                {"stage": "Notified", "key": "notified", "count": notified_count},
                {"stage": "Approved", "key": "approved", "count": approved_count},
                {"stage": "Posted", "key": "posted", "count": distinct_posted_clips},
            ],
            "outcomes": {
                "posted_publications": len(window_clips),
                "kept_local": kept_local_count,
                "archived": archived_count,
                "failed": failed_count,
            },
        },
        "cohorts": cohorts,
        "streamers": streamers_breakdown,
        "clips": window_clips,
    }

