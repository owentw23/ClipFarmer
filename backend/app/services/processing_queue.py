from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

import aiosqlite

from app.config import Settings
from app.db import open_connection, utcnow_iso
from app.services.neon_client import NeonClippingClient
from app.services.subprocess_compat import create_subprocess_exec
from app.storage import draft_clip_filename, slugify_streamer
from app.textfix import repair_text

logger = logging.getLogger(__name__)


async def ensure_chunk_error_column(conn: aiosqlite.Connection) -> None:
    """Idempotently add last_error column to recording_chunks if missing."""
    async with conn.execute("PRAGMA table_info(recording_chunks);") as cur:
        rows = await cur.fetchall()
    col_names = {row["name"] for row in rows}
    if "last_error" not in col_names:
        await conn.execute("ALTER TABLE recording_chunks ADD COLUMN last_error TEXT;")
        await conn.commit()


def _parse_iso_utc(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = Path(sys.executable).resolve().parent / f"ffmpeg{suffix}"
    if candidate.is_file():
        return str(candidate)
    return "ffmpeg"


async def _extract_thumbnail_jpeg(video_path: Path, thumb_path: Path) -> bool:
    """Extract a JPEG cover frame at t=1.0s, falling back to t=0s on short clips."""
    ffmpeg_bin = _find_ffmpeg()
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    for seek_sec in ("1.0", "0"):
        proc = await create_subprocess_exec(
            ffmpeg_bin,
            "-y",
            "-ss",
            seek_sec,
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(thumb_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0 and thumb_path.is_file() and thumb_path.stat().st_size > 0:
            return True
    thumb_path.unlink(missing_ok=True)
    return False


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Return True if fn accepts the named keyword argument or **kwargs."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD
        for p in params
    )


class ProcessingQueueWorker:
    """Single serialized GPU worker loop for NeonClipping + copywriter + Drive upload."""

    def __init__(
        self,
        settings: Settings,
        neon_client: NeonClippingClient | None = None,
        copywriter: Any | None = None,
        google_drive: Any | None = None,
        on_chunk_drafts_ready: Callable[[int, list[int]], Awaitable[Any] | Any] | None = None,
    ) -> None:
        self.settings = settings
        self.neon_client = neon_client or NeonClippingClient(
            base_url=settings.neonclipping_url,
            data_dir=settings.neonclipping_data_dir,
        )
        if copywriter is None:
            from app.services.copywriter import CopywriterService

            copywriter = CopywriterService(settings)
        if google_drive is None:
            from app.services.google_drive import GoogleDriveService

            google_drive = GoogleDriveService(settings)

        self.copywriter = copywriter
        self.google_drive = google_drive
        self.on_chunk_drafts_ready = on_chunk_drafts_ready

        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued_set: set[int] = set()
        self._gpu_lock = asyncio.Lock()
        self._running_chunk_id: int | None = None
        self._paused: bool = False
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def gpu_lock(self) -> asyncio.Lock:
        """Shared GPU lock used by both chunk queue and Studio analysis/export jobs."""
        return self._gpu_lock

    def pause(self) -> None:
        """Pause dispatching new queued chunks (active GPU job finishes cleanly)."""
        self._paused = True
        self._resume_event.clear()

    def resume(self) -> None:
        """Resume dispatching queued chunks."""
        self._paused = False
        self._resume_event.set()

    def enqueue_chunk(self, chunk_id: int) -> bool:
        """Enqueue chunk_id if not already queued or actively processing."""
        cid = int(chunk_id)
        if cid in self._queued_set or cid == self._running_chunk_id:
            return False
        self._queued_set.add(cid)
        self._queue.put_nowait(cid)
        return True

    def status(self) -> dict[str, Any]:
        """Return current queue state."""
        return {
            "running_chunk_id": self._running_chunk_id,
            "paused": self._paused,
            "queued_count": self._queue.qsize(),
        }

    async def start(self) -> None:
        """Recover stale 'processing' chunks, re-enqueue 'queued' chunks, and start worker loop."""
        if self._task is not None and not self._task.done():
            return

        conn = await open_connection(self.settings.db_path)
        try:
            await ensure_chunk_error_column(conn)
            await conn.execute(
                "UPDATE recording_chunks SET status = 'queued' WHERE status = 'processing'"
            )
            await conn.commit()
            async with conn.execute(
                "SELECT id FROM recording_chunks WHERE status = 'queued' ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await conn.close()

        for row in rows:
            self.enqueue_chunk(int(row["id"]))

        self._stop_event.clear()
        self._task = asyncio.create_task(self._worker_loop(), name="processing-queue")

    async def stop(self) -> None:
        """Stop the background worker loop cleanly."""
        self._stop_event.set()
        self._resume_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._resume_event.wait()
                if self._stop_event.is_set():
                    break
                chunk_id = await self._queue.get()
                try:
                    # Wait again if pause() happened while awaiting queue.get()
                    await self._resume_event.wait()
                    if self._stop_event.is_set():
                        self._queued_set.discard(chunk_id)
                        break
                    await self.process_chunk(chunk_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("ProcessingQueueWorker failed for chunk %s: %s", chunk_id, exc)
                finally:
                    self._queued_set.discard(chunk_id)
                    self._queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("ProcessingQueueWorker loop error: %s", exc)
                await asyncio.sleep(1.0)

    def _build_llm_extra_env(self) -> dict[str, str] | None:
        api_key = (self.settings.llm_api_key or "").strip()
        base_url = (self.settings.llm_base_url or "").strip()
        if not api_key or "/api/coding/v3" in base_url.lower():
            return None
        return {
            "NEONCLIPPING_LLM_BASE_URL": base_url,
            "NEONCLIPPING_LLM_MODEL": (self.settings.llm_model or "").strip(),
            "NEONCLIPPING_LLM_KEY": api_key,
        }

    async def process_chunk(self, chunk_id: int) -> dict[str, Any]:
        """Run the full serialized NeonClipping -> FFmpeg thumb -> Copywriter -> Drive pipeline."""
        cid = int(chunk_id)
        async with self._gpu_lock:
            self._running_chunk_id = cid
            staging_dir: Path | None = None
            try:
                conn = await open_connection(self.settings.db_path)
                try:
                    await ensure_chunk_error_column(conn)
                    async with conn.execute(
                        """
                        SELECT rc.*,
                               s.slug AS streamer_slug,
                               s.name AS streamer_name,
                               s.platform AS streamer_platform,
                               s.clip_opts_json AS clip_opts_json,
                               ss.title AS session_title
                        FROM recording_chunks rc
                        JOIN streamers s ON s.id = rc.streamer_id
                        LEFT JOIN stream_sessions ss ON ss.id = rc.session_id
                        WHERE rc.id = ?
                        """,
                        (cid,),
                    ) as cur:
                        chunk_row = await cur.fetchone()

                    if chunk_row is None:
                        raise LookupError(f"Recording chunk {cid} not found")

                    await conn.execute(
                        "UPDATE recording_chunks SET status = 'processing', last_error = NULL WHERE id = ?",
                        (cid,),
                    )

                    exclude_intervals: list[tuple[float, float]] | None = None
                    merged_from_id = chunk_row["merged_from_chunk_id"]
                    if merged_from_id is not None:
                        async with conn.execute(
                            """
                            SELECT start_sec, end_sec
                            FROM clip_drafts
                            WHERE chunk_id = ?
                              AND start_sec IS NOT NULL
                              AND end_sec IS NOT NULL
                            ORDER BY id ASC
                            """,
                            (int(merged_from_id),),
                        ) as ex_cur:
                            ex_rows = await ex_cur.fetchall()
                        if ex_rows:
                            exclude_intervals = [
                                (float(r["start_sec"]), float(r["end_sec"])) for r in ex_rows
                            ]

                    await conn.commit()
                finally:
                    await conn.close()

                chunk_path = Path(str(chunk_row["file_path"]))
                if not chunk_path.is_file():
                    raise FileNotFoundError(f"Recording chunk file missing: {chunk_path}")

                clip_opts: dict[str, Any] = {}
                try:
                    clip_opts = json.loads(chunk_row["clip_opts_json"] or "{}")
                except (ValueError, TypeError):
                    clip_opts = {}

                neon_data_dir = Path(self.settings.neonclipping_data_dir)
                neon_data_dir.mkdir(parents=True, exist_ok=True)
                staging_dir = Path(
                    tempfile.mkdtemp(prefix=f"chunk_{cid}_", dir=str(neon_data_dir))
                )

                extra_env = self._build_llm_extra_env()
                cam_auto = bool(clip_opts.get("cam_auto", True))
                raw_cam_box = clip_opts.get("cam_box")
                custom_cam = (
                    raw_cam_box
                    if (not cam_auto and isinstance(raw_cam_box, dict))
                    else None
                )
                raw_min_score = clip_opts.get("min_score")
                min_score = float(
                    raw_min_score if raw_min_score is not None else self.settings.vlm_min_score
                )
                extra_pipeline_kwargs: dict[str, Any] = {}
                sibling_chat = chunk_path.with_name(f"{chunk_path.stem}.chat.jsonl")
                if sibling_chat.is_file():
                    extra_pipeline_kwargs["chat_path"] = sibling_chat
                priors_file = neon_data_dir / "boundary_priors.json"
                if priors_file.is_file():
                    extra_pipeline_kwargs["boundary_priors_path"] = priors_file
                pipeline_res = await self.neon_client.arefine_and_export_with_turbo(
                    chunk_path,
                    output_dir=staging_dir,
                    count=max(1, min(10, int(clip_opts.get("clip_count") or 3))),
                    min_len=float(clip_opts.get("min_len", 30)),
                    max_len=float(clip_opts.get("max_len", 60)),
                    cam_auto=cam_auto,
                    cam=custom_cam,
                    layout=str(clip_opts.get("layout", "stack")),
                    fallback_layout=str(clip_opts.get("fallback_layout", "blur")),
                    caption_style=str(clip_opts.get("caption_style", "hormozi")),
                    language=clip_opts.get("language"),
                    min_score=min_score,
                    exclude_intervals=exclude_intervals,
                    extra_env=extra_env,
                    **extra_pipeline_kwargs,
                )

                clips = list(pipeline_res.get("clips") or [])
                if not clips:
                    logger.info(
                        "Chunk %s produced 0 clips meeting quality floor; marking processed.",
                        cid,
                    )
                drafts_dir = Path(self.settings.drafts_dir)
                drafts_dir.mkdir(parents=True, exist_ok=True)

                streamer_slug = slugify_streamer(
                    str(chunk_row["streamer_slug"] or chunk_row["streamer_name"] or "streamer")
                )
                streamer_name = str(chunk_row["streamer_name"] or streamer_slug)
                streamer_platform = str(chunk_row["streamer_platform"] or "")
                session_title = repair_text(str(chunk_row["session_title"] or ""))
                chunk_index = int(chunk_row["chunk_index"])
                chunk_dt = _parse_iso_utc(chunk_row["started_at"] or chunk_row["created_at"])
                clip_date = chunk_dt.strftime("%Y-%m-%d")

                created_clip_ids: list[int] = []
                conn = await open_connection(self.settings.db_path)
                try:
                    for idx, clip in enumerate(clips, start=1):
                        draft_number = int(clip.get("index") or idx)
                        src_mp4 = Path(str(clip["output_mp4"]))
                        if not src_mp4.is_file():
                            raise FileNotFoundError(f"Exported clip MP4 missing: {src_mp4}")

                        dest_mp4 = drafts_dir / draft_clip_filename(
                            streamer_slug, chunk_dt, chunk_index, draft_number, ext="mp4"
                        )
                        shutil.move(str(src_mp4), str(dest_mp4))

                        src_srt_raw = clip.get("output_srt")
                        if src_srt_raw:
                            src_srt = Path(str(src_srt_raw))
                            if src_srt.is_file():
                                dest_srt = drafts_dir / draft_clip_filename(
                                    streamer_slug, chunk_dt, chunk_index, draft_number, ext="srt"
                                )
                                shutil.move(str(src_srt), str(dest_srt))

                        dest_jpg = drafts_dir / draft_clip_filename(
                            streamer_slug, chunk_dt, chunk_index, draft_number, ext="jpg"
                        )
                        thumb_ok = await _extract_thumbnail_jpeg(dest_mp4, dest_jpg)
                        thumbnail_path = str(dest_jpg) if thumb_ok else None

                        copy_kwargs: dict[str, Any] = {
                            "transcript": str(clip.get("transcript") or ""),
                            "neon_title": str(clip.get("title") or ""),
                            "neon_hook": str(clip.get("hook") or ""),
                            "streamer_name": streamer_name,
                            "stream_title": session_title,
                            "platform": streamer_platform,
                        }
                        visual_summary = str(clip.get("visual_summary") or "").strip()
                        if visual_summary or _accepts_kwarg(
                            self.copywriter.generate_youtube_copy, "visual_summary"
                        ):
                            copy_kwargs["visual_summary"] = visual_summary
                        copy_res = await self.copywriter.generate_youtube_copy(**copy_kwargs)
                        title = str(
                            copy_res.get("title") or clip.get("title") or f"Clip {draft_number}"
                        )
                        description = str(copy_res.get("description") or "")
                        hashtags = [str(t) for t in (clip_opts.get("hashtags") or []) if str(t).strip()]
                        if hashtags:
                            # The streamer's default hashtags go under the copy; editable per clip.
                            description = "\n\n".join(p for p in (description, " ".join(hashtags)) if p)
                        features = clip.get("features")
                        tags = list(copy_res.get("tags") or [])

                        drive_file_id: str | None = None
                        drive_url: str | None = None
                        try:
                            drive_res = await self.google_drive.upload_clip(
                                conn,
                                dest_mp4,
                                streamer_name=streamer_name,
                                clip_date=clip_date,
                                display_name=dest_mp4.name,
                                share_anyone=True,
                            )
                            if drive_res:
                                drive_file_id = drive_res.get("drive_file_id")
                                drive_url = drive_res.get("drive_url")
                        except Exception as drive_exc:
                            logger.warning(
                                "Non-fatal Google Drive upload failure for %s: %s",
                                dest_mp4.name,
                                drive_exc,
                            )

                        start_sec = (
                            float(clip["start"]) if clip.get("start") is not None else None
                        )
                        end_sec = float(clip["end"]) if clip.get("end") is not None else None
                        if clip.get("duration") is not None:
                            duration_sec = float(clip["duration"])
                        elif start_sec is not None and end_sec is not None:
                            duration_sec = round(end_sec - start_sec, 3)
                        else:
                            duration_sec = None
                        score = float(clip["score"]) if clip.get("score") is not None else None

                        now = utcnow_iso()
                        cur = await conn.execute(
                            """
                            INSERT INTO clip_drafts (
                                streamer_id, session_id, chunk_id, draft_number,
                                title, description, tags_json, file_path, thumbnail_path,
                                drive_file_id, drive_url, start_sec, end_sec, duration_sec,
                                score, features_json, status, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                            """,
                            (
                                int(chunk_row["streamer_id"]),
                                int(chunk_row["session_id"]),
                                cid,
                                draft_number,
                                title,
                                description,
                                json.dumps(tags),
                                str(dest_mp4),
                                thumbnail_path,
                                drive_file_id,
                                drive_url,
                                start_sec,
                                end_sec,
                                duration_sec,
                                score,
                                json.dumps(features) if isinstance(features, dict) and features else None,
                                now,
                                now,
                            ),
                        )
                        # Commit each draft at once: holding the write lock across the next
                        # clip's copywriter and Drive upload blocked every other API write.
                        await conn.commit()
                        created_clip_ids.append(int(cur.lastrowid))

                    await conn.execute(
                        "UPDATE recording_chunks SET status = 'processed', last_error = NULL WHERE id = ?",
                        (cid,),
                    )
                    await conn.commit()
                finally:
                    await conn.close()

                if self.on_chunk_drafts_ready is not None and created_clip_ids:
                    cb_res = self.on_chunk_drafts_ready(cid, created_clip_ids)
                    if inspect.isawaitable(cb_res):
                        await cb_res

                return {
                    "ok": True,
                    "chunk_id": cid,
                    "status": "processed",
                    "clip_ids": created_clip_ids,
                    "count": len(created_clip_ids),
                }
            except Exception as exc:
                logger.exception("Error processing chunk %s: %s", cid, exc)
                err_conn = await open_connection(self.settings.db_path)
                try:
                    await ensure_chunk_error_column(err_conn)
                    # RetentionGC deletes 'failed' chunks once delete_after passes. Push it to
                    # at least now + chunk_retention_seconds so a manual retry stays possible.
                    async with err_conn.execute(
                        "SELECT delete_after FROM recording_chunks WHERE id = ?",
                        (cid,),
                    ) as da_cur:
                        da_row = await da_cur.fetchone()
                    retry_until = datetime.now(timezone.utc) + timedelta(
                        seconds=int(self.settings.chunk_retention_seconds)
                    )
                    existing_raw = da_row["delete_after"] if da_row is not None else None
                    if existing_raw:
                        retry_until = max(retry_until, _parse_iso_utc(existing_raw))
                    await err_conn.execute(
                        """
                        UPDATE recording_chunks
                        SET status = 'failed', last_error = ?, delete_after = ?
                        WHERE id = ?
                        """,
                        (str(exc)[:1000], retry_until.isoformat(), cid),
                    )
                    await err_conn.commit()
                finally:
                    await err_conn.close()
                raise
            finally:
                self._running_chunk_id = None
                self._queued_set.discard(cid)
                if staging_dir is not None and staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)
