from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import inspect
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from app.config import Settings
from app.db import open_connection, utcnow_iso
from app.services.chat_recorder import LiveChatRecorder
from app.services.subprocess_compat import create_subprocess_exec
from app.storage import raw_chunk_filename, slugify_streamer, streamer_preview_path
from app.textfix import repair_text

logger = logging.getLogger(__name__)

_SEGMENT_INDEX_RE = re.compile(r"_chunk(\d+)\.mkv$")


def find_binary(name: str) -> str:
    """Resolve an executable from PATH or the active Python environment Scripts dir."""
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if sys.platform == "win32" else ""
    candidate = Path(sys.executable).resolve().parent / f"{name}{suffix}"
    if candidate.is_file():
        return str(candidate)
    raise FileNotFoundError(f"Required executable '{name}' was not found on PATH or in venv Scripts")


def compute_min_full_sec(
    chunk_duration_seconds: int | float,
    tolerance_sec: float | None = None,
) -> float:
    """Compute the minimum duration threshold (seconds) for a chunk to count as full."""
    duration = max(1.0, float(chunk_duration_seconds))
    if tolerance_sec is not None:
        return max(0.5, duration - float(tolerance_sec))
    # Use 5s tolerance for standard >=30s chunks, and proportional tolerance for short test chunks.
    auto_tol = 5.0 if duration >= 30.0 else min(5.0, max(0.25, duration * 0.25))
    return max(0.5, duration - auto_tol)


def _creationflags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


async def terminate_process_cleanly(
    proc: asyncio.subprocess.Process,
    *,
    send_q: bool = False,
    timeout: float = 5.0,
) -> None:
    """Stop a subprocess gracefully (stdin 'q' / CTRL_BREAK_EVENT on Windows) before terminate/kill."""
    if proc.returncode is not None:
        return

    if send_q and proc.stdin is not None:
        try:
            proc.stdin.write(b"q\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass

    try:
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass

    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass

    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def probe_duration(path: str | Path) -> float:
    """Probe media duration in seconds using ffprobe."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Media file does not exist: {target}")

    ffprobe_bin = find_binary("ffprobe")
    proc = await create_subprocess_exec(
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        err = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffprobe failed for {target}: {err or proc.returncode}")

    raw_val = stdout_bytes.decode("utf-8", errors="replace").strip()
    if raw_val and raw_val.upper() != "N/A":
        try:
            return float(raw_val)
        except ValueError:
            pass

    # Fallback to stream duration if format duration is N/A (e.g. certain Matroska files)
    stream_proc = await create_subprocess_exec(
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "stream=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    s_out, s_err = await stream_proc.communicate()
    if stream_proc.returncode == 0:
        durations: list[float] = []
        for line in s_out.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and line.upper() != "N/A":
                try:
                    durations.append(float(line))
                except ValueError:
                    continue
        if durations:
            return max(durations)

    err = s_err.decode("utf-8", errors="replace").strip()
    raise RuntimeError(f"Could not parse duration from ffprobe output for {target}: {raw_val!r} {err}")


async def remux_to_mp4(src_mkv: str | Path, dst_mp4: str | Path) -> Path:
    """Losslessly remux a Matroska segment to MP4 with faststart metadata."""
    src = Path(src_mkv)
    dst = Path(dst_mp4)
    if not src.is_file():
        raise FileNotFoundError(f"Source segment not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = find_binary("ffmpeg")
    proc = await create_subprocess_exec(
        ffmpeg_bin,
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        err = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg remux failed ({src} -> {dst}): {err[-500:]}")
    return dst


def _escape_concat_path(path: Path) -> str:
    resolved = path.resolve().as_posix()
    return resolved.replace("'", r"'\''")


async def concat_chunks_lossless(
    chunk_a: str | Path,
    chunk_b: str | Path,
    dst_mp4: str | Path,
) -> Path:
    """Losslessly concatenate two media chunks via the FFmpeg concat demuxer."""
    path_a = Path(chunk_a)
    path_b = Path(chunk_b)
    dst = Path(dst_mp4)
    if not path_a.is_file():
        raise FileNotFoundError(f"First chunk not found: {path_a}")
    if not path_b.is_file():
        raise FileNotFoundError(f"Second chunk not found: {path_b}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = find_binary("ffmpeg")
    temp_files: list[Path] = []

    try:
        # FFmpeg concat demuxer requires matching container timebases/extradata;
        # losslessly remux any non-MP4 input (such as a raw .mkv tail) to a temp .mp4 first.
        inputs: list[Path] = []
        for idx, src in enumerate((path_a, path_b)):
            if src.suffix.lower() == ".mp4":
                inputs.append(src)
            else:
                fd_tmp, tmp_name = tempfile.mkstemp(
                    prefix=f"concat_src{idx}_",
                    suffix=".mp4",
                    dir=str(dst.parent),
                )
                os.close(fd_tmp)
                tmp_mp4 = Path(tmp_name)
                temp_files.append(tmp_mp4)
                await remux_to_mp4(src, tmp_mp4)
                inputs.append(tmp_mp4)

        fd, list_name = tempfile.mkstemp(prefix="concat_", suffix=".txt", dir=str(dst.parent))
        list_path = Path(list_name)
        temp_files.append(list_path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for item in inputs:
                fh.write(f"file '{_escape_concat_path(item)}'\n")

        proc = await create_subprocess_exec(
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dst),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
            err = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg concat failed ({path_a} + {path_b} -> {dst}): {err[-500:]}")
        return dst
    finally:
        for tmp in temp_files:
            tmp.unlink(missing_ok=True)


def build_twitch_capture_commands(
    stream_url: str,
    segment_pattern: str | Path,
    chunk_duration_seconds: int,
) -> tuple[list[str], list[str]]:
    """Build (streamlink_cmd, ffmpeg_segment_cmd) for Twitch capture."""
    streamlink_bin = find_binary("streamlink")
    ffmpeg_bin = find_binary("ffmpeg")
    producer_cmd = [
        streamlink_bin,
        "--twitch-disable-ads",
        "--stdout",
        stream_url,
        "best",
    ]
    ffmpeg_cmd = [
        ffmpeg_bin,
        "-nostats",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(int(chunk_duration_seconds)),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "matroska",
        "-segment_start_number",
        "1",
        str(segment_pattern),
    ]
    return producer_cmd, ffmpeg_cmd


def build_youtube_capture_commands(
    stream_url: str,
    segment_pattern: str | Path,
    chunk_duration_seconds: int,
    cookies_file: str = "",
) -> tuple[list[str], list[str]]:
    """Build (producer_cmd, ffmpeg_segment_cmd) for YouTube live capture."""
    ffmpeg_bin = find_binary("ffmpeg")
    producer_cmd: list[str]
    try:
        streamlink_bin = find_binary("streamlink")
        producer_cmd = [streamlink_bin, "--stdout", stream_url, "best"]
    except FileNotFoundError:
        ytdlp_bin = find_binary("yt-dlp")
        producer_cmd = [ytdlp_bin, "--no-playlist", "--no-part", "-o", "-"]
        if cookies_file and Path(cookies_file).is_file():
            producer_cmd.extend(["--cookies", cookies_file])
        producer_cmd.append(stream_url)

    ffmpeg_cmd = [
        ffmpeg_bin,
        "-nostats",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(int(chunk_duration_seconds)),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "matroska",
        "-segment_start_number",
        "1",
        str(segment_pattern),
    ]
    return producer_cmd, ffmpeg_cmd


async def _invoke_on_chunk_queued(
    callback: Callable[[int], Awaitable[Any] | Any] | None,
    chunk_id: int,
) -> None:
    if callback is None:
        return
    result = callback(chunk_id)
    if inspect.isawaitable(result):
        await result


async def _insert_recording_chunk(
    db_path: str | Path,
    *,
    session_id: int,
    streamer_id: int,
    chunk_index: int,
    file_path: str,
    duration_sec: float | None,
    merged_with_prev: int,
    merged_from_chunk_id: int | None,
    status: str,
    started_at: str | None,
    completed_at: str | None,
    delete_after: str | None,
) -> int:
    conn = await open_connection(db_path)
    try:
        cur = await conn.execute(
            """
            INSERT INTO recording_chunks (
                session_id, streamer_id, chunk_index, file_path, duration_sec,
                merged_with_prev, merged_from_chunk_id, status,
                started_at, completed_at, delete_after, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                streamer_id,
                chunk_index,
                file_path,
                duration_sec,
                merged_with_prev,
                merged_from_chunk_id,
                status,
                started_at,
                completed_at,
                delete_after,
                utcnow_iso(),
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)
    finally:
        await conn.close()


async def _get_last_full_chunk(db_path: str | Path, session_id: int) -> dict[str, Any] | None:
    conn = await open_connection(db_path)
    try:
        async with conn.execute(
            """
            SELECT id, file_path, chunk_index, duration_sec, status
            FROM recording_chunks
            WHERE session_id = ?
              AND status NOT IN ('discarded_short', 'deleted')
            ORDER BY chunk_index DESC, id DESC
            LIMIT 1
            """,
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        await conn.close()


async def readable_duration(path: str | Path) -> tuple[float, Path]:
    """Duration of a segment, recovering one FFmpeg never closed (stalled or killed).

    An unclosed Matroska file has no duration in its header, so ffprobe reports N/A and the
    footage used to be thrown away as empty. Remuxing it rewrites the index; the returned
    path is the file to use from here on.
    """
    src = Path(path)
    try:
        dur = await probe_duration(src)
        if dur > 0:
            return dur, src
    except Exception:
        pass
    if not src.is_file() or src.stat().st_size < 1_000_000:
        return 0.0, src
    recovered = src.with_name(f"{src.stem}_recovered.mp4")
    try:
        await remux_to_mp4(src, recovered)
        dur = await probe_duration(recovered)
    except Exception as exc:
        logger.warning("Could not recover segment %s: %s", src, exc)
        recovered.unlink(missing_ok=True)
        return 0.0, src
    src.unlink(missing_ok=True)
    return dur, recovered


async def process_full_chunk(
    *,
    db_path: str | Path,
    session_id: int,
    streamer_id: int,
    streamer_slug: str,
    src_mkv: str | Path,
    chunk_index: int,
    chunk_started_at: datetime,
    settings: Settings,
    on_chunk_queued: Callable[[int], Awaitable[Any] | Any] | None = None,
) -> dict[str, Any]:
    """Remux a completed full chunk to canonical .mp4, record DB row, and trigger queue callback."""
    started_utc = (
        chunk_started_at.astimezone(timezone.utc)
        if chunk_started_at.tzinfo is not None
        else chunk_started_at.replace(tzinfo=timezone.utc)
    )
    dst_name = raw_chunk_filename(streamer_slug, started_utc, chunk_index)
    dst_mp4 = Path(settings.raw_recordings_dir) / dst_name
    await remux_to_mp4(src_mkv, dst_mp4)
    Path(src_mkv).unlink(missing_ok=True)

    duration_sec = await probe_duration(dst_mp4)
    completed_dt = datetime.now(timezone.utc)
    delete_after_dt = started_utc + timedelta(seconds=int(settings.chunk_retention_seconds))

    chunk_id = await _insert_recording_chunk(
        db_path,
        session_id=session_id,
        streamer_id=streamer_id,
        chunk_index=chunk_index,
        file_path=str(dst_mp4),
        duration_sec=duration_sec,
        merged_with_prev=0,
        merged_from_chunk_id=None,
        status="queued",
        started_at=started_utc.isoformat(),
        completed_at=completed_dt.isoformat(),
        delete_after=delete_after_dt.isoformat(),
    )
    await _invoke_on_chunk_queued(on_chunk_queued, chunk_id)
    return {
        "action": "queued_full",
        "chunk_id": chunk_id,
        "chunk_index": chunk_index,
        "file_path": str(dst_mp4),
        "duration_sec": duration_sec,
        "status": "queued",
        "started_at": started_utc.isoformat(),
        "delete_after": delete_after_dt.isoformat(),
    }


async def finalize_stream_end(
    *,
    db_path: str | Path,
    session_id: int,
    streamer_id: int,
    streamer_slug: str,
    tail_mkv_path: str | Path,
    chunk_index: int,
    chunk_started_at: datetime,
    settings: Settings,
    last_full_chunk: dict[str, Any] | None = None,
    min_full_sec: float | None = None,
    min_standalone_sec: float | None = None,
    on_chunk_queued: Callable[[int], Awaitable[Any] | Any] | None = None,
    keep_partial: bool = False,
    min_partial_sec: float | None = None,
) -> dict[str, Any]:
    """Finalize the last segment when a stream ends (full chunk, standalone, discarded short, or tail merge).

    keep_partial: the recorder was stopped by the user, stalled or failed, so a partial
    recording of at least min_partial_sec is clipped instead of discarded.
    """
    tail_path = Path(tail_mkv_path)
    started_utc = (
        chunk_started_at.astimezone(timezone.utc)
        if chunk_started_at.tzinfo is not None
        else chunk_started_at.replace(tzinfo=timezone.utc)
    )
    threshold = (
        float(min_full_sec)
        if min_full_sec is not None
        else compute_min_full_sec(settings.chunk_duration_seconds)
    )

    tail_duration = 0.0
    if tail_path.is_file() and tail_path.stat().st_size > 0:
        tail_duration, tail_path = await readable_duration(tail_path)

    if tail_duration <= 0.0 and tail_path.is_file() and tail_path.stat().st_size >= 1_000_000:
        # Real footage that neither ffprobe nor a remux could read: never delete it. It is
        # listed as 'unreadable' (cleanup leaves that status alone) for a manual look.
        chunk_id = await _insert_recording_chunk(
            db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            chunk_index=chunk_index,
            file_path=str(tail_path),
            duration_sec=None,
            merged_with_prev=0,
            merged_from_chunk_id=None,
            status="unreadable",
            started_at=started_utc.isoformat(),
            completed_at=datetime.now(timezone.utc).isoformat(),
            delete_after=None,
        )
        logger.warning("Kept unreadable recording %s for inspection", tail_path)
        return {"action": "kept_unreadable", "chunk_id": chunk_id, "chunk_index": chunk_index, "file_path": str(tail_path)}

    if tail_duration >= threshold:
        return await process_full_chunk(
            db_path=db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            streamer_slug=streamer_slug,
            src_mkv=tail_path,
            chunk_index=chunk_index,
            chunk_started_at=started_utc,
            settings=settings,
            on_chunk_queued=on_chunk_queued,
        )

    prior_chunk = last_full_chunk
    if prior_chunk is None:
        prior_chunk = await _get_last_full_chunk(db_path, session_id)

    partial_floor = float(
        min_partial_sec if min_partial_sec is not None else settings.min_partial_seconds
    )
    if prior_chunk is None and keep_partial and tail_duration >= partial_floor:
        res = await process_full_chunk(
            db_path=db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            streamer_slug=streamer_slug,
            src_mkv=tail_path,
            chunk_index=chunk_index,
            chunk_started_at=started_utc,
            settings=settings,
            on_chunk_queued=on_chunk_queued,
        )
        res["action"] = "queued_partial"
        return res

    raw_standalone = float(
        min_standalone_sec if min_standalone_sec is not None else settings.min_standalone_chunk_seconds
    )
    effective_standalone = raw_standalone if raw_standalone < threshold else threshold
    if prior_chunk is None and tail_duration >= effective_standalone:
        res = await process_full_chunk(
            db_path=db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            streamer_slug=streamer_slug,
            src_mkv=tail_path,
            chunk_index=chunk_index,
            chunk_started_at=started_utc,
            settings=settings,
            on_chunk_queued=on_chunk_queued,
        )
        res["action"] = "queued_standalone"
        return res

    if prior_chunk is None:
        tail_path.unlink(missing_ok=True)
        completed_dt = datetime.now(timezone.utc)
        chunk_id = await _insert_recording_chunk(
            db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            chunk_index=chunk_index,
            file_path=str(tail_path),
            duration_sec=tail_duration,
            merged_with_prev=0,
            merged_from_chunk_id=None,
            status="discarded_short",
            started_at=started_utc.isoformat(),
            completed_at=completed_dt.isoformat(),
            delete_after=None,
        )
        return {
            "action": "discarded_short",
            "chunk_id": chunk_id,
            "chunk_index": chunk_index,
            "file_path": str(tail_path),
            "duration_sec": tail_duration,
            "status": "discarded_short",
        }

    # Partial tail chunk with an existing prior full chunk N -> lossless merge
    if tail_duration <= 0.0 or not tail_path.is_file():
        tail_path.unlink(missing_ok=True)
        return {
            "action": "discarded_empty_tail",
            "chunk_id": None,
            "chunk_index": chunk_index,
            "duration_sec": 0.0,
        }

    chunk_n_id = int(prior_chunk["id"])
    chunk_n_path = Path(prior_chunk["file_path"])
    merged_mp4 = chunk_n_path.with_name(f"{chunk_n_path.stem}_merged.mp4")

    await concat_chunks_lossless(chunk_n_path, tail_path, merged_mp4)
    tail_path.unlink(missing_ok=True)

    merged_duration = await probe_duration(merged_mp4)
    now_dt = datetime.now(timezone.utc)
    delete_after_dt = now_dt + timedelta(seconds=int(settings.chunk_retention_seconds))

    merged_chunk_id = await _insert_recording_chunk(
        db_path,
        session_id=session_id,
        streamer_id=streamer_id,
        chunk_index=chunk_index,
        file_path=str(merged_mp4),
        duration_sec=merged_duration,
        merged_with_prev=1,
        merged_from_chunk_id=chunk_n_id,
        status="queued",
        started_at=started_utc.isoformat(),
        completed_at=now_dt.isoformat(),
        delete_after=delete_after_dt.isoformat(),
    )
    await _invoke_on_chunk_queued(on_chunk_queued, merged_chunk_id)
    return {
        "action": "merged_tail",
        "chunk_id": merged_chunk_id,
        "merged_from_chunk_id": chunk_n_id,
        "chunk_index": chunk_index,
        "file_path": str(merged_mp4),
        "duration_sec": merged_duration,
        "status": "queued",
        "delete_after": delete_after_dt.isoformat(),
    }


@dataclass
class _ActiveRecording:
    streamer_id: int
    streamer_name: str
    streamer_slug: str
    platform: str
    stream_url: str
    external_stream_id: str
    title: str
    session_id: int
    started_at: str
    channel_ref: str = ""
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    chunks_completed: int = 0
    # Why the stop event was set: stream_ended (watcher saw it go offline),
    # stopped_by_user (Stop recording), backend_stopped (shutdown).
    stop_reason: str = "stream_ended"
    raw_dir: Path | None = None
    prefix: str = ""


async def capture_preview_frame(video_path: str | Path, dest_jpg: str | Path) -> bool:
    """Extract a single 16:9 JPEG reference frame from a local recording file."""
    src = Path(video_path)
    dst = Path(dest_jpg)
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = find_binary("ffmpeg")
    tmp_jpg = dst.with_name(f"{dst.stem}.tmp.{os.getpid()}.jpg")
    try:
        for seek_args in (["-ss", "2"], []):
            proc = await create_subprocess_exec(
                ffmpeg_bin,
                "-y",
                "-v",
                "error",
                *seek_args,
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(tmp_jpg),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=20.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                continue
            if proc.returncode == 0 and tmp_jpg.is_file() and tmp_jpg.stat().st_size > 0:
                os.replace(tmp_jpg, dst)
                return True
        return False
    except Exception as exc:
        logger.warning("Preview frame capture failed for %s: %s", src, exc)
        return False
    finally:
        tmp_jpg.unlink(missing_ok=True)


async def capture_live_preview_frame(
    stream_url: str,
    dest_jpg: str | Path,
    *,
    platform: str = "twitch",
    cookies_file: str = "",
) -> bool:
    """Grab one live JPEG frame via streamlink -> ffmpeg without starting a full recording."""
    dst = Path(dest_jpg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_jpg = dst.with_name(f"{dst.stem}.live.{os.getpid()}.jpg")
    ffmpeg_bin = find_binary("ffmpeg")
    if platform == "twitch":
        producer_cmd, _ = build_twitch_capture_commands(stream_url, tmp_jpg, 60)
    else:
        producer_cmd, _ = build_youtube_capture_commands(
            stream_url, tmp_jpg, 60, cookies_file=cookies_file
        )
    ffmpeg_cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(tmp_jpg),
    ]
    producer_proc: Any = None
    ffmpeg_proc: Any = None
    r_fd, w_fd = os.pipe()
    try:
        try:
            producer_proc = await create_subprocess_exec(
                *producer_cmd,
                stdout=w_fd,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=_creationflags(),
            )
            ffmpeg_proc = await create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=r_fd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_creationflags(),
            )
        finally:
            for fd in (r_fd, w_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            await asyncio.wait_for(ffmpeg_proc.communicate(), timeout=25.0)
        except asyncio.TimeoutError:
            pass
        if tmp_jpg.is_file() and tmp_jpg.stat().st_size > 0:
            os.replace(tmp_jpg, dst)
            return True
        return False
    except Exception as exc:
        logger.warning("Live preview frame capture failed for %s: %s", stream_url, exc)
        return False
    finally:
        for p in (producer_proc, ffmpeg_proc):
            if p is not None and p.returncode is None:
                try:
                    p.kill()
                    await asyncio.wait_for(p.wait(), timeout=3.0)
                except Exception:
                    pass
        tmp_jpg.unlink(missing_ok=True)


def _coerce_stream_info(stream_info: Any) -> dict[str, Any]:
    if isinstance(stream_info, dict):
        data = dict(stream_info)
    else:
        data = {
            "streamer_id": getattr(stream_info, "streamer_id"),
            "streamer_name": getattr(stream_info, "streamer_name", ""),
            "streamer_slug": getattr(stream_info, "streamer_slug", ""),
            "platform": getattr(stream_info, "platform", ""),
            "channel_ref": getattr(stream_info, "channel_ref", ""),
            "stream_url": getattr(stream_info, "stream_url", ""),
            "external_stream_id": getattr(stream_info, "external_stream_id", ""),
            "title": getattr(stream_info, "title", ""),
        }
    name = str(data.get("streamer_name") or data.get("name") or f"streamer-{data['streamer_id']}")
    slug = str(data.get("streamer_slug") or slugify_streamer(name))
    data["streamer_id"] = int(data["streamer_id"])
    data["streamer_name"] = name
    data["streamer_slug"] = slug
    data["platform"] = str(data.get("platform") or "twitch").lower()
    data["channel_ref"] = str(data.get("channel_ref") or "")
    data["stream_url"] = str(data.get("stream_url") or "")
    data["external_stream_id"] = str(data.get("external_stream_id") or "")
    data["title"] = repair_text(str(data.get("title") or ""))
    return data


def _chunk_chat_path(raw_dir: Path, streamer_slug: str, chunk_started_at: datetime, chunk_index: int) -> Path:
    started_utc = (
        chunk_started_at.astimezone(timezone.utc)
        if chunk_started_at.tzinfo is not None
        else chunk_started_at.replace(tzinfo=timezone.utc)
    )
    mp4_name = raw_chunk_filename(streamer_slug, started_utc, chunk_index)
    return raw_dir / f"{Path(mp4_name).stem}.chat.jsonl"


def _log_tail(*paths: Path, limit: int = 600) -> str:
    """Last lines of the recorder's log files, for an error message."""
    parts: list[str] = []
    for path in paths:
        try:
            data = path.read_bytes()[-4000:].decode("utf-8", errors="replace")
        except OSError:
            continue
        lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
        if lines:
            parts.append(f"{path.stem.rsplit('_', 1)[-1]}: " + " | ".join(lines[-3:]))
    return repair_text(" / ".join(parts))[-limit:]


# Sessions that end this way are not failures, and their status is "completed".
_CLEAN_ENDS = {"stream_ended", "stopped_by_user", "backend_stopped"}


class RecorderManager:
    """Manages concurrent live stream capture tasks and chunk finalization."""

    def __init__(
        self,
        settings: Settings,
        on_chunk_queued: Callable[[int], Awaitable[Any] | Any] | None = None,
        min_full_tolerance_sec: float | None = None,
        on_session_problem: Callable[[dict[str, Any]], Awaitable[Any] | Any] | None = None,
    ) -> None:
        self.settings = settings
        self.on_chunk_queued = on_chunk_queued
        self.on_session_problem = on_session_problem
        self.min_full_tolerance_sec = min_full_tolerance_sec
        self._active: dict[int, _ActiveRecording] = {}
        self._lock = asyncio.Lock()
        # Consecutive failed sessions per streamer; reset by a session that ends cleanly.
        self._failures: dict[int, int] = {}

    def is_recording(self, streamer_id: int) -> bool:
        """Return True if a recording task is currently active for streamer_id."""
        rec = self._active.get(int(streamer_id))
        return rec is not None and rec.task is not None and not rec.task.done()

    def failure_count(self, streamer_id: int) -> int:
        return self._failures.get(int(streamer_id), 0)

    def active_segment_path(self, streamer_id: int) -> Path | None:
        """Return the newest segment file for an active recording, if present."""
        rec = self._active.get(int(streamer_id))
        if rec is None or rec.raw_dir is None or not rec.prefix:
            return None
        segs = self._discover_segments(rec.raw_dir, rec.prefix)
        return segs[-1][1] if segs else None

    async def _capture_session_preview(self, seg_path: Path, streamer_id: int) -> None:
        try:
            await capture_preview_frame(
                seg_path, streamer_preview_path(self.settings, streamer_id)
            )
        except Exception as exc:
            logger.warning("Auto preview capture failed for streamer %s: %s", streamer_id, exc)

    def active_recordings(self) -> list[dict[str, Any]]:
        """Return metadata dictionaries for all currently active recordings."""
        items: list[dict[str, Any]] = []
        for rec in self._active.values():
            if rec.task is not None and rec.task.done():
                continue
            items.append(
                {
                    "streamer_id": rec.streamer_id,
                    "streamer_name": rec.streamer_name,
                    "streamer_slug": rec.streamer_slug,
                    "platform": rec.platform,
                    "stream_url": rec.stream_url,
                    "external_stream_id": rec.external_stream_id,
                    "title": rec.title,
                    "session_id": rec.session_id,
                    "started_at": rec.started_at,
                    "chunks_completed": rec.chunks_completed,
                }
            )
        return items

    async def start_recording(self, stream_info: Any) -> bool:
        """Start an asynchronous recording task for a live stream if not already active."""
        info = _coerce_stream_info(stream_info)
        streamer_id = info["streamer_id"]

        async with self._lock:
            existing = self._active.get(streamer_id)
            if existing is not None and existing.task is not None and not existing.task.done():
                return False

            started_dt = datetime.now(timezone.utc)
            started_iso = started_dt.isoformat()
            Path(self.settings.raw_recordings_dir).mkdir(parents=True, exist_ok=True)

            conn = await open_connection(self.settings.db_path)
            try:
                cur = await conn.execute(
                    """
                    INSERT INTO stream_sessions (
                        streamer_id, external_stream_id, title, started_at, status, created_at
                    )
                    VALUES (?, ?, ?, ?, 'recording', ?)
                    """,
                    (
                        streamer_id,
                        info["external_stream_id"] or None,
                        info["title"] or None,
                        started_iso,
                        started_iso,
                    ),
                )
                await conn.commit()
                session_id = int(cur.lastrowid)
            finally:
                await conn.close()

            rec = _ActiveRecording(
                streamer_id=streamer_id,
                streamer_name=info["streamer_name"],
                streamer_slug=info["streamer_slug"],
                platform=info["platform"],
                stream_url=info["stream_url"],
                external_stream_id=info["external_stream_id"],
                title=info["title"],
                session_id=session_id,
                started_at=started_iso,
                channel_ref=info["channel_ref"],
            )
            rec.task = asyncio.create_task(
                self._record_session(rec, started_dt),
                name=f"recorder-{rec.streamer_slug}-{streamer_id}",
            )
            self._active[streamer_id] = rec
            return True

    def request_stop(self, streamer_id: int, reason: str = "stream_ended") -> bool:
        """Signal an active recording to stop without waiting for finalization.

        The recording task keeps running in the background to remux, merge, and queue the
        last chunk. is_recording() stays True until that work finishes, and stop_all()
        still waits for it at shutdown. WatcherManager uses this so one stream's
        finalization does not hold up live checks for every other streamer.
        """
        rec = self._active.get(int(streamer_id))
        if rec is None or rec.task is None or rec.task.done():
            return False
        rec.stop_reason = reason
        rec.stop_event.set()
        return True

    async def stop_recording(self, streamer_id: int, reason: str = "stopped_by_user") -> bool:
        """Signal an active recording to stop cleanly and await finalization."""
        async with self._lock:
            rec = self._active.get(int(streamer_id))
        if rec is None:
            return False

        rec.stop_reason = reason
        rec.stop_event.set()
        if rec.task is not None:
            try:
                await rec.task
            except asyncio.CancelledError:
                pass
        return True

    async def stop_all(self) -> None:
        """Stop all active recordings cleanly and wait for finalization."""
        async with self._lock:
            recs = list(self._active.values())
        for rec in recs:
            rec.stop_reason = "backend_stopped"
            rec.stop_event.set()
        tasks = [rec.task for rec in recs if rec.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _build_capture_commands(
        self,
        rec: _ActiveRecording,
        segment_pattern: Path,
    ) -> tuple[list[str], list[str]]:
        chunk_secs = int(self.settings.chunk_duration_seconds)
        if rec.platform == "twitch":
            return build_twitch_capture_commands(rec.stream_url, segment_pattern, chunk_secs)
        return build_youtube_capture_commands(
            rec.stream_url,
            segment_pattern,
            chunk_secs,
            cookies_file=getattr(self.settings, "youtube_cookies_file", ""),
        )

    def _discover_segments(self, raw_dir: Path, prefix: str) -> list[tuple[int, Path]]:
        segments: list[tuple[int, Path]] = []
        for path in raw_dir.glob(f"{prefix}_chunk*.mkv"):
            match = _SEGMENT_INDEX_RE.search(path.name)
            if match:
                segments.append((int(match.group(1)), path))
        segments.sort(key=lambda item: item[0])
        return segments

    async def _update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        try:
            conn = await open_connection(self.settings.db_path)
            try:
                await conn.execute(
                    f"UPDATE stream_sessions SET {cols} WHERE id = ?",
                    (*fields.values(), session_id),
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception as exc:
            logger.exception("Failed updating stream_session %s: %s", session_id, exc)

    async def _finalize_segments(
        self,
        rec: _ActiveRecording,
        segments: list[tuple[int, Path]],
        chunk_start_times: dict[int, datetime],
        last_full_chunk: dict[str, Any] | None,
        min_full_sec: float,
        keep_partial: bool,
    ) -> None:
        """Queue every finished hour and settle the last, possibly partial, segment."""
        if not segments:
            return
        for idx, seg_path in segments[:-1]:
            c_start = chunk_start_times.get(idx, datetime.now(timezone.utc))
            dur, seg_path = await readable_duration(seg_path)
            if dur >= min_full_sec:
                res = await process_full_chunk(
                    db_path=self.settings.db_path,
                    session_id=rec.session_id,
                    streamer_id=rec.streamer_id,
                    streamer_slug=rec.streamer_slug,
                    src_mkv=seg_path,
                    chunk_index=idx,
                    chunk_started_at=c_start,
                    settings=self.settings,
                    on_chunk_queued=self.on_chunk_queued,
                )
                last_full_chunk = {"id": res["chunk_id"], "file_path": res["file_path"]}
                rec.chunks_completed += 1
            else:
                seg_path.unlink(missing_ok=True)

        tail_idx, tail_path = segments[-1]
        tail_start = chunk_start_times.get(tail_idx, datetime.now(timezone.utc))
        try:
            end_res = await finalize_stream_end(
                db_path=self.settings.db_path,
                session_id=rec.session_id,
                streamer_id=rec.streamer_id,
                streamer_slug=rec.streamer_slug,
                tail_mkv_path=tail_path,
                chunk_index=tail_idx,
                chunk_started_at=tail_start,
                settings=self.settings,
                last_full_chunk=last_full_chunk,
                min_full_sec=min_full_sec,
                on_chunk_queued=self.on_chunk_queued,
                keep_partial=keep_partial,
            )
            if end_res.get("action") in {"queued_full", "queued_standalone", "merged_tail", "queued_partial"}:
                rec.chunks_completed += 1
        except Exception as exc:
            logger.exception("Error finalizing tail segment %s: %s", tail_path, exc)

    async def _record_session(self, rec: _ActiveRecording, session_dt: datetime) -> None:
        raw_dir = Path(self.settings.raw_recordings_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{slugify_streamer(rec.streamer_slug)}_{session_dt.strftime('%Y-%m-%d_%H%M%S')}"
        rec.raw_dir = raw_dir
        rec.prefix = prefix
        segment_pattern = raw_dir / f"{prefix}_chunk%02d.mkv"

        # The recorders' output goes to log files. Piping it back and never reading it let the
        # pipe fill up, and then streamlink or FFmpeg froze on its next log line.
        log_dir = Path(self.settings.logs_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        source_log = log_dir / f"{prefix}_source.log"
        ffmpeg_log = log_dir / f"{prefix}_ffmpeg.log"
        await self._update_session(rec.session_id, log_path=str(source_log))

        producer_proc: Any = None
        ffmpeg_proc: Any = None
        end_reason = "stream_ended"
        error: str | None = None
        processed_indices: set[int] = set()
        last_full_chunk: dict[str, Any] | None = None
        chunk_start_times: dict[int, datetime] = {1: session_dt}
        min_full_sec = compute_min_full_sec(
            self.settings.chunk_duration_seconds,
            tolerance_sec=self.min_full_tolerance_sec,
        )
        stall_limit = max(10.0, float(self.settings.recording_stall_seconds))
        source_fh = open(source_log, "ab")
        ffmpeg_fh = open(ffmpeg_log, "ab")
        chat_recorder: LiveChatRecorder | None = None
        current_chat_chunk_idx = 1

        try:
            try:
                chat_recorder = LiveChatRecorder(
                    platform=rec.platform,
                    channel_ref=rec.channel_ref,
                    chat_path=_chunk_chat_path(raw_dir, rec.streamer_slug, session_dt, 1),
                    chunk_start_monotonic=time.monotonic(),
                    external_stream_id=rec.external_stream_id,
                    stream_url=rec.stream_url,
                )
                chat_recorder.start()
            except Exception as chat_exc:
                logger.debug("Chat recorder start failed (non-fatal): %s", chat_exc)
                chat_recorder = None

            producer_cmd, ffmpeg_cmd = self._build_capture_commands(rec, segment_pattern)
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            r_fd, w_fd = os.pipe()
            try:
                producer_proc = await create_subprocess_exec(
                    *producer_cmd,
                    stdout=w_fd,
                    stderr=source_fh,
                    creationflags=_creationflags(),
                    env=env,
                )
                ffmpeg_proc = await create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdin=r_fd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=ffmpeg_fh,
                    creationflags=_creationflags(),
                )
            finally:
                for fd in (r_fd, w_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            last_size = -1
            last_growth = time.monotonic()
            preview_captured = False
            while not rec.stop_event.is_set():
                segments = self._discover_segments(raw_dir, prefix)
                for idx, _ in segments:
                    if idx not in chunk_start_times:
                        chunk_start_times[idx] = datetime.now(timezone.utc)
                if segments and chat_recorder is not None:
                    newest_idx = segments[-1][0]
                    if newest_idx > current_chat_chunk_idx:
                        current_chat_chunk_idx = newest_idx
                        try:
                            c_dt = chunk_start_times.get(newest_idx, datetime.now(timezone.utc))
                            chat_recorder.rotate_chunk(
                                _chunk_chat_path(raw_dir, rec.streamer_slug, c_dt, newest_idx),
                                chunk_start_monotonic=time.monotonic(),
                            )
                        except Exception as chat_exc:
                            logger.debug("Chat recorder rotate failed (non-fatal): %s", chat_exc)

                # Any segment with a higher-numbered segment present on disk is closed by FFmpeg.
                for idx, seg_path in segments[:-1]:
                    if idx in processed_indices:
                        continue
                    processed_indices.add(idx)
                    c_start = chunk_start_times.get(idx, session_dt)
                    dur, seg_path = await readable_duration(seg_path)
                    if dur >= min_full_sec:
                        res = await process_full_chunk(
                            db_path=self.settings.db_path,
                            session_id=rec.session_id,
                            streamer_id=rec.streamer_id,
                            streamer_slug=rec.streamer_slug,
                            src_mkv=seg_path,
                            chunk_index=idx,
                            chunk_started_at=c_start,
                            settings=self.settings,
                            on_chunk_queued=self.on_chunk_queued,
                        )
                        last_full_chunk = {"id": res["chunk_id"], "file_path": res["file_path"]}
                        rec.chunks_completed += 1
                    else:
                        seg_path.unlink(missing_ok=True)

                if producer_proc.returncode is not None or ffmpeg_proc.returncode is not None:
                    p_code, f_code = producer_proc.returncode, ffmpeg_proc.returncode
                    if p_code in (0, None) and f_code in (0, None):
                        end_reason = "stream_ended"
                    else:
                        end_reason = "error"
                        who = "streamlink" if p_code not in (0, None) else "ffmpeg"
                        code = p_code if p_code not in (0, None) else f_code
                        error = f"{who} exited with code {code}. {_log_tail(source_log, ffmpeg_log)}".strip()
                    break

                # Stall watchdog: the newest segment must keep growing.
                current = segments[-1][1] if segments else None
                try:
                    size = current.stat().st_size if current is not None else 0
                except OSError:
                    size = 0
                if not preview_captured and current is not None and size >= 262_144:
                    preview_captured = True
                    asyncio.create_task(
                        self._capture_session_preview(current, rec.streamer_id),
                        name=f"preview-{rec.streamer_id}",
                    )
                now = time.monotonic()
                if size != last_size:
                    last_size, last_growth = size, now
                elif now - last_growth >= stall_limit:
                    end_reason = "stalled"
                    error = (
                        f"No new video for {int(stall_limit)} seconds, so the recorder was restarted. "
                        f"{_log_tail(source_log, ffmpeg_log)}"
                    ).strip()
                    break

                try:
                    await asyncio.wait_for(rec.stop_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

            if rec.stop_event.is_set():
                end_reason = rec.stop_reason

        except asyncio.CancelledError:
            end_reason = "cancelled"
            raise
        except Exception as exc:
            end_reason = "error"
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Recording session failed for streamer %s: %s", rec.streamer_id, exc)
        finally:
            if chat_recorder is not None:
                try:
                    chat_recorder.stop()
                except Exception as chat_exc:
                    logger.debug("Chat recorder stop failed (non-fatal): %s", chat_exc)
            if producer_proc is not None and producer_proc.returncode is None:
                await terminate_process_cleanly(producer_proc, send_q=False, timeout=3.0)
            if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
                # Wait briefly for EOF from producer pipe before sending CTRL_BREAK_EVENT
                try:
                    await asyncio.wait_for(ffmpeg_proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    await terminate_process_cleanly(ffmpeg_proc, send_q=False, timeout=5.0)
            for fh in (source_fh, ffmpeg_fh):
                try:
                    fh.close()
                except OSError:
                    pass

            remaining = [
                (idx, path)
                for idx, path in self._discover_segments(raw_dir, prefix)
                if idx not in processed_indices
            ]
            # A stream that ended on its own keeps the short-stream rule; anything that cut
            # the recording short (Stop, a stall, a crash, shutdown) keeps the partial footage.
            await self._finalize_segments(
                rec,
                remaining,
                chunk_start_times,
                last_full_chunk,
                min_full_sec,
                keep_partial=end_reason != "stream_ended",
            )

            if end_reason in _CLEAN_ENDS:
                status = "completed"
            elif end_reason == "cancelled":
                status = "stopped"
            else:
                status = "error"
            await self._update_session(
                rec.session_id,
                ended_at=utcnow_iso(),
                status=status,
                end_reason=end_reason,
                last_error=error,
            )
            self._active.pop(rec.streamer_id, None)

            if status == "error":
                count = self._failures.get(rec.streamer_id, 0) + 1
                self._failures[rec.streamer_id] = count
                await self._report_problem(
                    {
                        "streamer_id": rec.streamer_id,
                        "streamer_name": rec.streamer_name,
                        "platform": rec.platform,
                        "session_id": rec.session_id,
                        "end_reason": end_reason,
                        "error": error or "The recorder stopped unexpectedly.",
                        "failures": count,
                        "chunks_saved": rec.chunks_completed,
                    }
                )
            elif status == "completed":
                self._failures.pop(rec.streamer_id, None)

    async def _report_problem(self, problem: dict[str, Any]) -> None:
        if self.on_session_problem is None:
            return
        try:
            res = self.on_session_problem(problem)
            if inspect.isawaitable(res):
                await res
        except Exception as exc:
            logger.warning("Recording problem notification failed: %s", exc)

    async def recover_interrupted(self) -> list[dict[str, Any]]:
        """Finish sessions the backend left mid-recording (a hard kill or a crash).

        Their segments are recovered (FFmpeg never closed them), finished hours are queued,
        and a partial last segment is clipped if it is long enough.
        """
        conn = await open_connection(self.settings.db_path)
        try:
            async with conn.execute(
                """
                SELECT ss.id, ss.streamer_id, ss.started_at, ss.title,
                       s.name AS streamer_name, s.slug AS streamer_slug, s.platform
                FROM stream_sessions ss
                JOIN streamers s ON s.id = ss.streamer_id
                WHERE ss.status = 'recording'
                ORDER BY ss.id ASC
                """
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        finally:
            await conn.close()

        raw_dir = Path(self.settings.raw_recordings_dir)
        min_full_sec = compute_min_full_sec(
            self.settings.chunk_duration_seconds, tolerance_sec=self.min_full_tolerance_sec
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            if self.is_recording(int(row["streamer_id"])) and any(
                r.session_id == row["id"] for r in self._active.values()
            ):
                continue
            try:
                started = datetime.fromisoformat(str(row["started_at"]))
            except ValueError:
                continue
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            prefix = f"{slugify_streamer(str(row['streamer_slug']))}_{started.strftime('%Y-%m-%d_%H%M%S')}"
            segments = self._discover_segments(raw_dir, prefix)
            rec = _ActiveRecording(
                streamer_id=int(row["streamer_id"]),
                streamer_name=str(row["streamer_name"]),
                streamer_slug=str(row["streamer_slug"]),
                platform=str(row["platform"]),
                stream_url="",
                external_stream_id="",
                title=str(row["title"] or ""),
                session_id=int(row["id"]),
                started_at=str(row["started_at"]),
            )
            starts = {idx: started + timedelta(seconds=(idx - 1) * self.settings.chunk_duration_seconds) for idx, _ in segments}
            try:
                await self._finalize_segments(rec, segments, starts, None, min_full_sec, keep_partial=True)
            except Exception as exc:
                logger.exception("Recovering session %s failed: %s", row["id"], exc)
            note = (
                f"The backend stopped without finishing this recording; recovered {rec.chunks_completed} chunk(s)."
                if segments
                else "The backend stopped without finishing this recording; no footage was left on disk."
            )
            await self._update_session(
                rec.session_id,
                ended_at=utcnow_iso(),
                status="interrupted",
                end_reason="backend_stopped",
                last_error=note,
            )
            results.append({"session_id": rec.session_id, "recovered_chunks": rec.chunks_completed})
        return results
