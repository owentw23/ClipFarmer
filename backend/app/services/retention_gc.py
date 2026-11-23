from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import open_connection

logger = logging.getLogger(__name__)

# 'failed' chunks are eligible too: ProcessingQueueWorker pushes their delete_after to
# failure time + chunk_retention_seconds, which leaves a manual-retry window before deletion.
ELIGIBLE_STATUSES = ("processed", "failed", "failed_permanent")
LOCKING_CHILD_STATUSES = ("recording", "queued", "processing")


def _parse_iso_utc(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class RetentionGC:
    """Deletes expired raw recording chunks once processing and child merge locks clear."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_once(
        self,
        now: datetime | str | None = None,
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Execute a single GC pass and return deleted_chunk_ids and skipped_locked count."""
        now_dt = _parse_iso_utc(now) if now is not None else datetime.now(timezone.utc)
        target_db = db_path or self.settings.db_path

        deleted_chunk_ids: list[int] = []
        skipped_locked = 0

        conn = await open_connection(target_db)
        try:
            async with conn.execute(
                """
                SELECT DISTINCT merged_from_chunk_id
                FROM recording_chunks
                WHERE merged_from_chunk_id IS NOT NULL
                  AND status IN ('recording', 'queued', 'processing')
                """
            ) as cur:
                lock_rows = await cur.fetchall()
            locked_parent_ids = {int(row["merged_from_chunk_id"]) for row in lock_rows}

            async with conn.execute(
                """
                SELECT id, file_path, status, delete_after
                FROM recording_chunks
                WHERE delete_after IS NOT NULL
                  AND status != 'deleted'
                ORDER BY id ASC
                """
            ) as cur:
                candidates = await cur.fetchall()

            for row in candidates:
                chunk_id = int(row["id"])
                status = str(row["status"])
                try:
                    delete_after_dt = _parse_iso_utc(row["delete_after"])
                except ValueError:
                    continue

                if delete_after_dt > now_dt:
                    continue

                if status not in ELIGIBLE_STATUSES or chunk_id in locked_parent_ids:
                    skipped_locked += 1
                    continue

                file_path = Path(str(row["file_path"]))
                try:
                    file_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Failed to delete raw chunk file %s: %s", file_path, exc)
                    skipped_locked += 1
                    continue

                await conn.execute(
                    "UPDATE recording_chunks SET status = 'deleted' WHERE id = ?",
                    (chunk_id,),
                )
                deleted_chunk_ids.append(chunk_id)

            if deleted_chunk_ids:
                await conn.commit()
        finally:
            await conn.close()

        return {
            "deleted_chunk_ids": deleted_chunk_ids,
            "skipped_locked": skipped_locked,
        }

    async def _loop(self) -> None:
        interval = max(1.0, float(getattr(self.settings, "gc_interval_seconds", 60)))
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("RetentionGC run_once error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        """Start the periodic background retention GC loop."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="retention-gc")

    async def stop(self) -> None:
        """Stop the periodic background retention GC loop cleanly."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
