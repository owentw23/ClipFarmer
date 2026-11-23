from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import logging
from typing import Any

import aiosqlite
import httpx

from app.config import Settings
from app.db import open_connection, utcnow_iso
from app.services.google_oauth import GoogleOAuthError, get_valid_access_token

logger = logging.getLogger(__name__)

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
DATA_API_BATCH_SIZE = 50
ANALYTICS_BATCH_SIZE = 200
ANALYTICS_METRICS = (
    "views,engagedViews,estimatedMinutesWatched,"
    "averageViewDuration,averageViewPercentage,"
    "likes,comments,shares,subscribersGained,subscribersLost"
)


class MetricsCollectorError(RuntimeError):
    """Raised when YouTube metrics polling fails."""


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_analytics_rows(payload: dict[str, Any]) -> dict[str, dict[str, float | int]]:
    headers = payload.get("columnHeaders") or []
    rows = payload.get("rows") or []
    col_names = [str(col.get("name") or "") for col in headers]
    if "video" not in col_names:
        return {}
    video_idx = col_names.index("video")
    result: dict[str, dict[str, float | int]] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or video_idx >= len(row):
            continue
        vid = str(row[video_idx] or "").strip()
        if not vid:
            continue
        entry: dict[str, float | int] = {}
        for idx, name in enumerate(col_names):
            if name == "video" or idx >= len(row):
                continue
            val = row[idx]
            if name in {
                "estimatedMinutesWatched",
                "averageViewDuration",
                "averageViewPercentage",
            }:
                entry[name] = _to_float(val, 0.0)
            else:
                entry[name] = _to_int(val, 0)
        result[vid] = entry
    return result


class MetricsCollectorService:
    """Async poller for YouTube Data API v3 statistics and Analytics API v2 reports."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = 2,
        retry_base_delay: float = 0.5,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = max(0.0, float(retry_base_delay))
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._sync_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start periodic YouTube metrics polling loop."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="metrics-collector")
        logger.info(
            "MetricsCollectorService started (interval=%ss)",
            self.settings.metrics_poll_interval_sec,
        )

    async def stop(self) -> None:
        """Stop periodic YouTube metrics polling loop."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("MetricsCollectorService stopped")

    async def _run_loop(self) -> None:
        interval = max(60, int(self.settings.metrics_poll_interval_sec))
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.sync_all()
            except Exception as exc:
                logger.warning("Background metrics sync failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _send_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        params: dict[str, Any],
        headers: dict[str, str],
        failure_label: str,
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    if self.retry_base_delay > 0:
                        await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise MetricsCollectorError(f"{failure_label} transport error: {exc}") from exc

            if resp.status_code == 200:
                return resp

            if _is_retryable_status(resp.status_code) and attempt < self.max_retries:
                if self.retry_base_delay > 0:
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                continue

            raise MetricsCollectorError(
                f"{failure_label} failed ({resp.status_code}): {resp.text[:300]}"
            )

        raise MetricsCollectorError(f"{failure_label} exhausted retries")

    async def _fetch_data_stats(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        video_ids: list[str],
    ) -> dict[str, dict[str, int]]:
        stats_by_video: dict[str, dict[str, int]] = {}
        headers = {"Authorization": f"Bearer {access_token}"}
        for i in range(0, len(video_ids), DATA_API_BATCH_SIZE):
            batch = video_ids[i : i + DATA_API_BATCH_SIZE]
            resp = await self._send_with_retry(
                client,
                YOUTUBE_VIDEOS_URL,
                params={"part": "statistics", "id": ",".join(batch)},
                headers=headers,
                failure_label="YouTube Data API statistics",
            )
            payload = resp.json()
            for item in payload.get("items") or []:
                vid = str(item.get("id") or "").strip()
                stats = item.get("statistics") or {}
                if not vid:
                    continue
                stats_by_video[vid] = {
                    "views": _to_int(stats.get("viewCount"), 0),
                    "likes": _to_int(stats.get("likeCount"), 0),
                    "comments": _to_int(stats.get("commentCount"), 0),
                }
        return stats_by_video

    async def _fetch_analytics_stats(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        pubs: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, float | int]], dict[str, int], str | None]:
        today = datetime.now(timezone.utc).date()
        end_date_str = today.strftime("%Y-%m-%d")
        parsed_dates = [
            d
            for d in (
                _parse_iso_date(p.get("published_at")) or _parse_iso_date(p.get("created_at"))
                for p in pubs
            )
            if d is not None
        ]
        earliest = min(parsed_dates) - timedelta(days=1) if parsed_dates else (today - timedelta(days=90))
        if earliest > today:
            earliest = today
        start_date_str = earliest.strftime("%Y-%m-%d")

        headers = {"Authorization": f"Bearer {access_token}"}
        combined: dict[str, dict[str, float | int]] = {}
        shorts_views: dict[str, int] = {}
        last_error: str | None = None

        for batch_pubs in _chunked(pubs, ANALYTICS_BATCH_SIZE):
            video_ids = [str(p["youtube_video_id"]) for p in batch_pubs]
            video_filter = f"video=={','.join(video_ids)}"
            try:
                resp = await self._send_with_retry(
                    client,
                    YOUTUBE_ANALYTICS_URL,
                    params={
                        "ids": "channel==MINE",
                        "startDate": start_date_str,
                        "endDate": end_date_str,
                        "metrics": ANALYTICS_METRICS,
                        "dimensions": "video",
                        "filters": video_filter,
                        "maxResults": str(len(video_ids)),
                    },
                    headers=headers,
                    failure_label="YouTube Analytics API",
                )
                combined.update(_parse_analytics_rows(resp.json()))
            except MetricsCollectorError as exc:
                last_error = str(exc)
                logger.warning("YouTube Analytics fetch skipped (degrading cleanly): %s", exc)
                continue

            try:
                shorts_resp = await self._send_with_retry(
                    client,
                    YOUTUBE_ANALYTICS_URL,
                    params={
                        "ids": "channel==MINE",
                        "startDate": start_date_str,
                        "endDate": end_date_str,
                        "metrics": "views",
                        "dimensions": "video",
                        "filters": f"{video_filter};insightTrafficSourceType==SHORTS",
                        "maxResults": str(len(video_ids)),
                    },
                    headers=headers,
                    failure_label="YouTube Analytics Shorts feed",
                )
                for vid, row in _parse_analytics_rows(shorts_resp.json()).items():
                    shorts_views[vid] = _to_int(row.get("views"), 0)
            except MetricsCollectorError as exc:
                logger.warning("YouTube Analytics Shorts-feed query skipped: %s", exc)

        return combined, shorts_views, last_error

    async def sync_all(self, db: aiosqlite.Connection | None = None) -> dict[str, Any]:
        """Poll YouTube Data API v3 & Analytics API v2 for all active posted publications."""
        async with self._sync_lock:
            if db is not None:
                return await self._sync_with_db(db)
            conn = await open_connection(self.settings.db_path)
            try:
                return await self._sync_with_db(conn)
            finally:
                await conn.close()

    async def _sync_with_db(self, db: aiosqlite.Connection) -> dict[str, Any]:
        async with db.execute(
            """
            SELECT p.id AS publication_id,
                   p.clip_draft_id,
                   p.youtube_account_id,
                   p.youtube_video_id,
                   p.published_at,
                   p.created_at,
                   ya.channel_id,
                   ya.channel_title,
                   ya.scopes
            FROM publications p
            JOIN youtube_accounts ya ON ya.id = p.youtube_account_id
            WHERE p.status IN ('published', 'posted')
              AND p.youtube_video_id IS NOT NULL
              AND TRIM(p.youtube_video_id) != ''
              AND ya.status = 'active'
              AND COALESCE(ya.oauth_provider, 'google') != 'google_drive'
              AND ya.channel_id NOT LIKE 'drive-only:%'
            ORDER BY p.id ASC
            """
        ) as cur:
            rows = await cur.fetchall()

        pubs = [dict(r) for r in rows]
        captured_at = utcnow_iso()
        if not pubs:
            return {
                "synced_publications": 0,
                "synced_accounts": 0,
                "analytics_synced": 0,
                "captured_at": captured_at,
                "errors": [],
            }

        grouped: dict[int, list[dict[str, Any]]] = {}
        for pub in pubs:
            grouped.setdefault(int(pub["youtube_account_id"]), []).append(pub)

        pub_ids = [int(p["publication_id"]) for p in pubs]
        placeholders = ",".join("?" for _ in pub_ids)
        prior_by_pub: dict[int, dict[str, Any]] = {}
        async with db.execute(
            f"""
            SELECT ms.*
            FROM metric_snapshots ms
            JOIN (
                SELECT publication_id, MAX(id) AS max_id
                FROM metric_snapshots
                WHERE publication_id IN ({placeholders})
                GROUP BY publication_id
            ) latest ON latest.max_id = ms.id
            """,
            pub_ids,
        ) as cur:
            for row in await cur.fetchall():
                d = dict(row)
                prior_by_pub[int(d["publication_id"])] = d

        synced_count = 0
        synced_accounts = 0
        analytics_synced = 0
        errors: list[str] = []

        async def _run_with_client(client: httpx.AsyncClient) -> None:
            nonlocal synced_count, synced_accounts, analytics_synced
            for account_id, acc_pubs in grouped.items():
                try:
                    access_token = await get_valid_access_token(
                        db, self.settings, account_id, http_client=client
                    )
                except (GoogleOAuthError, Exception) as exc:
                    errors.append(f"account {account_id}: {exc}")
                    continue

                video_ids = list(dict.fromkeys(str(p["youtube_video_id"]).strip() for p in acc_pubs))
                data_stats: dict[str, dict[str, int]] = {}
                try:
                    data_stats = await self._fetch_data_stats(client, access_token, video_ids)
                except MetricsCollectorError as exc:
                    errors.append(f"account {account_id} data API: {exc}")

                analytics_stats, shorts_views, analytics_err = await self._fetch_analytics_stats(
                    client, access_token, acc_pubs
                )
                if analytics_err:
                    errors.append(f"account {account_id} analytics API: {analytics_err}")

                account_had_sync = False
                for pub in acc_pubs:
                    pub_id = int(pub["publication_id"])
                    vid = str(pub["youtube_video_id"]).strip()
                    d_stat = data_stats.get(vid)
                    a_stat = analytics_stats.get(vid)
                    if d_stat is None and a_stat is None:
                        continue

                    prior = prior_by_pub.get(pub_id) or {}
                    views = max(
                        int(d_stat["views"]) if d_stat is not None else 0,
                        _to_int(a_stat.get("views"), 0) if a_stat is not None else 0,
                    )
                    likes = max(
                        int(d_stat["likes"]) if d_stat is not None else 0,
                        _to_int(a_stat.get("likes"), 0) if a_stat is not None else 0,
                    )
                    comments = max(
                        int(d_stat["comments"]) if d_stat is not None else 0,
                        _to_int(a_stat.get("comments"), 0) if a_stat is not None else 0,
                    )

                    if a_stat is not None:
                        estimated_minutes_watched = _to_float(
                            a_stat.get("estimatedMinutesWatched"), 0.0
                        )
                        engaged_views = _to_int(a_stat.get("engagedViews"), 0)
                        shares = _to_int(a_stat.get("shares"), 0)
                        subscribers_gained = _to_int(a_stat.get("subscribersGained"), 0)
                        subscribers_lost = _to_int(a_stat.get("subscribersLost"), 0)
                        avg_view_duration_sec = _to_float(
                            a_stat.get("averageViewDuration"), 0.0
                        )
                        avg_view_percentage = _to_float(
                            a_stat.get("averageViewPercentage"), 0.0
                        )
                        shorts_feed_views = _to_int(shorts_views.get(vid), 0)
                        analytics_synced_at: str | None = captured_at
                        analytics_synced += 1
                    else:
                        # Preserve last synced Analytics figures when Analytics API is throttled/unavailable
                        estimated_minutes_watched = _to_float(
                            prior.get("estimated_minutes_watched"), 0.0
                        )
                        engaged_views = _to_int(prior.get("engaged_views"), 0)
                        shares = _to_int(prior.get("shares"), 0)
                        subscribers_gained = _to_int(prior.get("subscribers_gained"), 0)
                        subscribers_lost = _to_int(prior.get("subscribers_lost"), 0)
                        avg_view_duration_sec = _to_float(
                            prior.get("avg_view_duration_sec"), 0.0
                        )
                        avg_view_percentage = _to_float(
                            prior.get("avg_view_percentage"), 0.0
                        )
                        shorts_feed_views = _to_int(prior.get("shorts_feed_views"), 0)
                        analytics_synced_at = prior.get("analytics_synced_at")

                    await db.execute(
                        """
                        INSERT INTO metric_snapshots (
                            publication_id, views, likes, comments,
                            estimated_minutes_watched, engaged_views, shares,
                            subscribers_gained, subscribers_lost,
                            avg_view_duration_sec, avg_view_percentage,
                            shorts_feed_views, analytics_synced_at, captured_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pub_id,
                            views,
                            likes,
                            comments,
                            estimated_minutes_watched,
                            engaged_views,
                            shares,
                            subscribers_gained,
                            subscribers_lost,
                            avg_view_duration_sec,
                            avg_view_percentage,
                            shorts_feed_views,
                            analytics_synced_at,
                            captured_at,
                        ),
                    )
                    synced_count += 1
                    account_had_sync = True

                if account_had_sync:
                    synced_accounts += 1

            await db.commit()

        if self._http_client is not None:
            await _run_with_client(self._http_client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await _run_with_client(client)

        return {
            "synced_publications": synced_count,
            "synced_accounts": synced_accounts,
            "analytics_synced": analytics_synced,
            "captured_at": captured_at,
            "errors": errors,
        }
