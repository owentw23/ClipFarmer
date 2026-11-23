"""Regression tests for three pipeline fixes:

1. Failed chunks get a retry window, then RetentionGC deletes them (they used to stay forever).
2. A stream going offline does not block WatcherManager.poll_once on chunk finalization.
3. Cancelling or timing out NeonClipping's pipeline_runner kills its whole process tree.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings
from app.db import init_db, utcnow_iso
from app.security import generate_fernet_key
from app.services.neon_client import NeonClippingClient
from app.services.processing_queue import ProcessingQueueWorker
from app.services.recorder import RecorderManager, _ActiveRecording
from app.services.retention_gc import RetentionGC
from app.services.watcher_manager import WatcherManager


@pytest.fixture()
def env():
    with tempfile.TemporaryDirectory(prefix="clip_fixes_") as tmp:
        root = Path(tmp) / "Clips_Store"
        raw_dir = root / "raw recordings"
        drafts_dir = root / "drafts"
        neon_dir = root / "neon_tmp"
        db_file = root / "db" / "pipeline.sqlite3"
        for d in (raw_dir, drafts_dir, neon_dir, db_file.parent):
            d.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            clips_store_root=str(root),
            raw_recordings_dir=str(raw_dir),
            drafts_dir=str(drafts_dir),
            neonclipping_data_dir=str(neon_dir),
            db_path=str(db_file),
            llm_api_key="",
            token_encryption_key=generate_fernet_key(),
            enable_background_workers=False,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, Path(tmp)


def _seed_chunk(settings: Settings, *, status: str, delete_after: datetime, is_live: int = 0):
    raw = Path(settings.raw_recordings_dir) / "tarik_2026-11-22_120000_chunk01.mp4"
    raw.write_bytes(b"fake-raw-chunk")
    now = utcnow_iso()
    conn = sqlite3.connect(settings.db_path)
    try:
        sid = conn.execute(
            """
            INSERT INTO streamers (name, slug, platform, channel_url, channel_ref, auto_watch,
                                   clip_opts_json, is_live, created_at, updated_at)
            VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', 1, ?, ?, ?, ?)
            """,
            (json.dumps({}), is_live, now, now),
        ).lastrowid
        sess = conn.execute(
            "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'completed', ?)",
            (sid, now, now),
        ).lastrowid
        cid = conn.execute(
            """
            INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status,
                                          started_at, delete_after, created_at)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (sess, sid, str(raw), status, now, delete_after.isoformat(), now),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    return int(sid), int(cid), raw


def _chunk_row(settings: Settings, cid: int) -> sqlite3.Row:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM recording_chunks WHERE id = ?", (cid,)).fetchone()
    finally:
        conn.close()


# --- 1. Failed chunks ------------------------------------------------------------------


class _FailingNeon:
    async def arefine_and_export_with_turbo(self, *args, **kwargs):
        raise RuntimeError("CUDA out of memory")


def test_failed_chunk_gets_retry_window_then_is_garbage_collected(env) -> None:
    settings, _ = env
    # delete_after already in the past: before the fix a failure here would either be
    # kept forever (old GC) or deleted within a minute (naive fix).
    long_ago = datetime.now(timezone.utc) - timedelta(hours=5)
    _, cid, raw = _seed_chunk(settings, status="queued", delete_after=long_ago)

    worker = ProcessingQueueWorker(
        settings, neon_client=_FailingNeon(), copywriter=object(), google_drive=object()
    )
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        asyncio.run(worker.process_chunk(cid))

    row = _chunk_row(settings, cid)
    assert row["status"] == "failed"
    pushed = datetime.fromisoformat(row["delete_after"])
    min_expected = datetime.now(timezone.utc) + timedelta(
        seconds=settings.chunk_retention_seconds - 60
    )
    assert pushed >= min_expected

    gc = RetentionGC(settings)
    within_window = asyncio.run(gc.run_once(now=datetime.now(timezone.utc)))
    assert within_window["deleted_chunk_ids"] == []
    assert raw.is_file()

    after_window = asyncio.run(gc.run_once(now=pushed + timedelta(seconds=1)))
    assert after_window["deleted_chunk_ids"] == [cid]
    assert not raw.exists()
    assert _chunk_row(settings, cid)["status"] == "deleted"


def test_failure_keeps_a_later_existing_delete_after(env) -> None:
    settings, _ = env
    far_future = datetime.now(timezone.utc) + timedelta(days=2)
    _, cid, _ = _seed_chunk(settings, status="queued", delete_after=far_future)

    worker = ProcessingQueueWorker(
        settings, neon_client=_FailingNeon(), copywriter=object(), google_drive=object()
    )
    with pytest.raises(RuntimeError):
        asyncio.run(worker.process_chunk(cid))

    assert datetime.fromisoformat(_chunk_row(settings, cid)["delete_after"]) == far_future


# --- 2. Offline does not block the watcher ---------------------------------------------


class _AllOfflineTwitch:
    async def check_channels(self, logins):
        return {}


def test_offline_transition_does_not_wait_for_finalization(env) -> None:
    settings, _ = env
    sid, _, _ = _seed_chunk(
        settings,
        status="processed",
        delete_after=datetime.now(timezone.utc) + timedelta(hours=2),
        is_live=1,
    )
    finalize_sec = 2.0

    async def scenario() -> None:
        recorder = RecorderManager(settings)
        rec = _ActiveRecording(
            streamer_id=sid,
            streamer_name="Tarik",
            streamer_slug="tarik",
            platform="twitch",
            stream_url="https://www.twitch.tv/tarik",
            external_stream_id="1",
            title="",
            session_id=1,
            started_at=utcnow_iso(),
        )

        async def fake_session() -> None:
            await rec.stop_event.wait()
            await asyncio.sleep(finalize_sec)  # remux / tail-merge stand-in
            recorder._active.pop(sid, None)

        rec.task = asyncio.create_task(fake_session())
        recorder._active[sid] = rec

        wm = WatcherManager(
            settings,
            on_online=recorder.start_recording,
            on_offline=recorder.request_stop,
            twitch_watcher=_AllOfflineTwitch(),
            is_recording=recorder.is_recording,
        )
        t0 = time.monotonic()
        await wm.poll_once()
        elapsed = time.monotonic() - t0

        assert elapsed < finalize_sec / 2, f"poll_once blocked for {elapsed:.2f}s"
        assert rec.stop_event.is_set()
        assert recorder.is_recording(sid)  # finalization still running in background

        await recorder.stop_all()  # shutdown still waits for finalization
        assert not recorder.is_recording(sid)

    asyncio.run(scenario())


def test_request_stop_without_active_recording_returns_false(env) -> None:
    settings, _ = env
    assert RecorderManager(settings).request_stop(999) is False


# --- 3. NeonClipping runner is killed on cancel / timeout ------------------------------

_SLOW_RUNNER = """
import os, subprocess, sys, time, pathlib
data_dir = pathlib.Path(sys.argv[sys.argv.index('--data-dir') + 1])
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
(data_dir / 'pids.txt').write_text(f"{os.getpid()} {child.pid}")
time.sleep(120)
"""


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
        ).stdout
        return str(pid) in out
    try:
        import os

        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _slow_client(tmp: Path) -> tuple[NeonClippingClient, Path, Path]:
    engine = tmp / "engine"
    engine.mkdir()
    (engine / "pipeline_runner.py").write_text(_SLOW_RUNNER, encoding="utf-8")
    data_dir = tmp / "neon_data"
    data_dir.mkdir()
    chunk = tmp / "chunk.mp4"
    chunk.write_bytes(b"x")
    client = NeonClippingClient(
        data_dir=data_dir, engine_dir=engine, python_executable=sys.executable
    )
    return client, data_dir, chunk


async def _wait_for_pids(pid_file: Path) -> list[int]:
    for _ in range(200):
        if pid_file.is_file() and pid_file.read_text().strip():
            return [int(p) for p in pid_file.read_text().split()]
        await asyncio.sleep(0.05)
    raise AssertionError("runner never wrote its PIDs")


def _assert_all_dead(pids: list[int]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.2)
    assert not any(_pid_alive(p) for p in pids), f"orphaned runner processes: {pids}"


def test_cancel_kills_runner_process_tree(env) -> None:
    _, tmp = env
    client, data_dir, chunk = _slow_client(tmp)

    async def scenario() -> list[int]:
        task = asyncio.create_task(client.arefine_and_export_with_turbo(chunk, cam_auto=False))
        pids = await _wait_for_pids(data_dir / "pids.txt")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pids

    pids = asyncio.run(scenario())
    assert len(pids) == 2
    _assert_all_dead(pids)


def test_timeout_kills_runner_process_tree(env) -> None:
    _, tmp = env
    client, data_dir, chunk = _slow_client(tmp)

    async def scenario() -> None:
        with pytest.raises(TimeoutError, match="timed out"):
            await client.arefine_and_export_with_turbo(chunk, timeout=3.0)

    asyncio.run(scenario())
    _assert_all_dead([int(p) for p in (data_dir / "pids.txt").read_text().split()])


# --- 4. Telegram Approve does not block the bot ----------------------------------------

import httpx  # noqa: E402

from app.services.telegram_bot import TelegramBotService  # noqa: E402


def _seed_clips(settings: Settings, count: int = 2) -> tuple[int, list[int]]:
    sid, cid, _ = _seed_chunk(
        settings, status="processed", delete_after=datetime.now(timezone.utc) + timedelta(hours=2)
    )
    now = utcnow_iso()
    conn = sqlite3.connect(settings.db_path)
    try:
        clip_ids = [
            int(
                conn.execute(
                    """
                    INSERT INTO clip_drafts (streamer_id, chunk_id, draft_number, title, file_path,
                                             status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'notified', ?, ?)
                    """,
                    (sid, cid, n, f"Clip {n}", f"C:/nope/{n}.mp4", now, now),
                ).lastrowid
            )
            for n in range(1, count + 1)
        ]
        conn.commit()
    finally:
        conn.close()
    return cid, clip_ids


class _SlowPublisher:
    """Stands in for YouTubePublisherService: records calls and takes `delay` per clip."""

    def __init__(self, settings: Settings, delay: float) -> None:
        self.settings = settings
        self.delay = delay
        self.calls: list[int] = []

    async def publish_clip(self, db, clip_id, *, privacy_override=None, account_ids=None):
        self.calls.append(clip_id)
        now = utcnow_iso()
        await db.execute(
            """
            INSERT INTO publications (clip_draft_id, privacy_status, status, created_at, updated_at)
            VALUES (?, 'public', 'uploading', ?, ?)
            """,
            (clip_id, now, now),
        )
        await db.commit()
        await asyncio.sleep(self.delay)
        await db.execute(
            "UPDATE publications SET status = 'published' WHERE clip_draft_id = ?", (clip_id,)
        )
        await db.execute("UPDATE clip_drafts SET status = 'posted' WHERE id = ?", (clip_id,))
        await db.commit()
        return {"succeeded": 1, "failed": 0, "publications": []}


def _tap(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "data": data,
            "message": {"chat": {"id": 1001}, "message_id": 77},
        },
    }


def _tg_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"telegram_bot_token": "123:abc", "telegram_allowed_chat_ids": "1001"}
    )


def test_approve_replies_immediately_and_bot_stays_responsive(env) -> None:
    settings, _ = env
    settings = _tg_settings(settings)
    _, (c1, c2) = _seed_clips(settings)
    answers: list[tuple[float, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/answerCallbackQuery"):
            answers.append((time.monotonic(), json.loads(request.content)["text"]))
        return httpx.Response(200, json={"ok": True, "result": True})

    upload_sec = 2.0

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = _SlowPublisher(settings, delay=upload_sec)
            bot = TelegramBotService(settings, publisher=publisher, http_client=client)

            t0 = time.monotonic()
            ap = await bot.handle_update(_tap(1, f"ap:{c1}"))
            assert ap["queued_count"] == 1
            assert time.monotonic() - t0 < upload_sec / 2, "Approve waited for the upload"

            # During the upload: a repeat tap must not upload twice, and other presses work.
            again = await bot.handle_update(_tap(2, f"ap:{c1}"))
            assert again["queued_count"] == 0 and again["skipped_count"] == 1
            rj = await bot.handle_update(_tap(3, f"rj:{c2}"))
            assert rj["rejected_count"] == 1
            assert time.monotonic() - t0 < upload_sec / 2

            jobs = await bot.wait_for_publishes()
            assert [j["posted_count"] for j in jobs] == [1]
            assert publisher.calls == [c1]

    asyncio.run(scenario())
    assert [text for _, text in answers] == [
        "Publishing 1 clip(s) to YouTube…",
        "Already publishing",
        "Marked 1 clip(s) as kept_local",
    ]
    conn = sqlite3.connect(settings.db_path)
    try:
        assert conn.execute("SELECT status FROM clip_drafts WHERE id = ?", (c1,)).fetchone()[0] == "posted"
        assert conn.execute("SELECT status FROM clip_drafts WHERE id = ?", (c2,)).fetchone()[0] == "kept_local"
    finally:
        conn.close()


def test_shutdown_during_upload_marks_clip_failed_for_reapproval(env) -> None:
    settings, _ = env
    settings = _tg_settings(settings)
    _, (c1, c2) = _seed_clips(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": True})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = _SlowPublisher(settings, delay=60.0)
            bot = TelegramBotService(settings, publisher=publisher, http_client=client)
            res = await bot.handle_update(_tap(1, f"aa:{_chunk_of(settings, c1)}"))
            assert res["queued_count"] == 2
            for _ in range(100):
                if publisher.calls:
                    break
                await asyncio.sleep(0.02)
            t0 = time.monotonic()
            await bot.stop()
            assert time.monotonic() - t0 < 5

    asyncio.run(scenario())
    conn = sqlite3.connect(settings.db_path)
    try:
        statuses = dict(conn.execute("SELECT id, status FROM clip_drafts").fetchall())
        assert statuses == {c1: "failed", c2: "failed"}
        pub = conn.execute("SELECT status, error_message FROM publications").fetchone()
        assert pub == ("failed", "Interrupted by backend shutdown")
    finally:
        conn.close()


def _chunk_of(settings: Settings, clip_id: int) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return int(conn.execute("SELECT chunk_id FROM clip_drafts WHERE id = ?", (clip_id,)).fetchone()[0])
    finally:
        conn.close()
