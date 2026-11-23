"""Background scheduler that publishes scheduled clip drafts when their time arrives."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from app.config import Settings
from app.db import open_connection, utcnow_iso
from app.services.youtube_publisher import YouTubePublisherService

logger = logging.getLogger(__name__)


def parse_utc_datetime(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string into timezone-aware UTC datetime."""
    if not raw or not str(raw).strip():
        return None
    normalized = str(raw).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PublishSchedulerWorker:
    """Periodically checks for scheduled clips due for publishing and uploads them to YouTube."""

    def __init__(
        self,
        settings: Settings,
        *,
        publisher: YouTubePublisherService | None = None,
        poll_interval_sec: float = 30.0,
    ) -> None:
        self.settings = settings
        self.publisher = publisher or YouTubePublisherService(settings)
        self.poll_interval_sec = max(1.0, float(poll_interval_sec))
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="publish-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Publish all scheduled clips whose scheduled_publish_at <= now."""
        async with self._lock:
            now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            published_ids: list[int] = []
            failed_ids: list[int] = []

            conn = await open_connection(self.settings.db_path)
            try:
                async with conn.execute(
                    """
                    SELECT id, scheduled_publish_at, scheduled_privacy_status
                    FROM clip_drafts
                    WHERE status = 'scheduled'
                      AND scheduled_publish_at IS NOT NULL
                    ORDER BY scheduled_publish_at ASC, id ASC
                    """
                ) as cur:
                    rows = await cur.fetchall()

                due: list[tuple[int, str | None]] = []
                for row in rows:
                    sched_dt = parse_utc_datetime(row["scheduled_publish_at"])
                    if sched_dt is not None and sched_dt <= now_utc:
                        due.append(
                            (
                                int(row["id"]),
                                str(row["scheduled_privacy_status"]).strip()
                                if row["scheduled_privacy_status"]
                                else None,
                            )
                        )

                for clip_id, privacy_override in due:
                    # Atomically claim so manual Post Now or overlapping ticks never double-post.
                    claim_cur = await conn.execute(
                        """
                        UPDATE clip_drafts
                        SET status = 'approved',
                            scheduled_publish_at = NULL,
                            scheduled_privacy_status = NULL,
                            updated_at = ?
                        WHERE id = ? AND status = 'scheduled'
                        """,
                        (utcnow_iso(), clip_id),
                    )
                    await conn.commit()
                    if claim_cur.rowcount != 1:
                        continue

                    try:
                        res = await self.publisher.publish_clip(
                            conn,
                            clip_id,
                            privacy_override=privacy_override,
                        )
                        if res.get("status") == "posted":
                            published_ids.append(clip_id)
                        else:
                            failed_ids.append(clip_id)
                    except Exception as exc:
                        logger.warning("Scheduled publish failed for clip %s: %s", clip_id, exc)
                        await conn.execute(
                            "UPDATE clip_drafts SET status = 'failed', updated_at = ? WHERE id = ?",
                            (utcnow_iso(), clip_id),
                        )
                        await conn.commit()
                        failed_ids.append(clip_id)
            finally:
                await conn.close()

            return {
                "checked_at": now_utc.isoformat(),
                "published_ids": published_ids,
                "failed_ids": failed_ids,
            }

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("PublishSchedulerWorker poll error: %s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_sec)
            except asyncio.TimeoutError:
                continue

