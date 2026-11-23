"""Recorder stall/partial/hold handling, ranking-learning sync, clip settings and JSON errors."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

from fastapi.testclient import TestClient
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, utcnow_iso
from app.main import create_app
from app.security import generate_fernet_key
from app.services.learning import LearningService, desired_ratings
from app.services.recorder import RecorderManager
from app.services.watcher_manager import WatcherManager
from app.textfix import repair_text


@pytest.fixture()
def env():
    with tempfile.TemporaryDirectory(prefix="clip_hold_") as tmp:
        root = Path(tmp) / "Clips_Store"
        for d in ("raw recordings", "drafts", "neon_tmp", "db"):
            (root / d).mkdir(parents=True, exist_ok=True)
        settings = Settings(
            clips_store_root=str(root),
            raw_recordings_dir=str(root / "raw recordings"),
            drafts_dir=str(root / "drafts"),
            neonclipping_data_dir=str(root / "neon_tmp"),
            db_path=str(root / "db" / "pipeline.sqlite3"),
            token_encryption_key=generate_fernet_key(),
            enable_background_workers=False,
            chunk_duration_seconds=30,
            recording_stall_seconds=10,
            min_partial_seconds=1,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings


def _add_streamer(settings: Settings, *, live: int = 0, held: int = 0, slug: str = "lacy") -> int:
    now = utcnow_iso()
    conn = sqlite3.connect(settings.db_path)
    try:
        sid = conn.execute(
            """
            INSERT INTO streamers (name, slug, platform, channel_url, channel_ref, auto_watch,
                                   clip_opts_json, is_live, hold_until_offline, created_at, updated_at)
            VALUES (?, ?, 'twitch', ?, ?, 1, '{}', ?, ?, ?, ?)
            """,
            (slug.title(), slug, f"https://twitch.tv/{slug}", slug, live, held, now, now),
        ).lastrowid
        conn.commit()
        return int(sid)
    finally:
        conn.close()


def _rows(settings: Settings, sql: str, args=()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# --- recorder ------------------------------------------------------------------------


class _SilentRecorder(RecorderManager):
    """Both 'recorder' processes start and then produce nothing, like the frozen ones."""

    def _build_capture_commands(self, rec, segment_pattern):
        idle = [sys.executable, "-c", "import time; time.sleep(120)"]
        return idle, idle


def test_stalled_recording_is_stopped_reported_and_logged(env) -> None:
    settings = env
    sid = _add_streamer(settings, live=1)
    problems: list[dict] = []

    async def scenario() -> None:
        rec = _SilentRecorder(settings, on_session_problem=problems.append)
        assert await rec.start_recording({"streamer_id": sid, "streamer_name": "Lacy", "platform": "twitch"})
        for _ in range(100):
            await asyncio.sleep(0.25)
            if not rec.is_recording(sid):
                break
        assert not rec.is_recording(sid), "watchdog never stopped the stalled recorder"

    asyncio.run(scenario())
    session = _rows(settings, "SELECT * FROM stream_sessions")[0]
    assert session["status"] == "error"
    assert session["end_reason"] == "stalled"
    assert "No new video for 10 seconds" in session["last_error"]
    assert Path(session["log_path"]).is_file()
    assert problems and problems[0]["end_reason"] == "stalled" and problems[0]["failures"] == 1


class _TestPatternRecorder(RecorderManager):
    """Feeds a real FFmpeg test pattern into the real segment muxer."""

    def _build_capture_commands(self, rec, segment_pattern):
        _, ffmpeg_cmd = super()._build_capture_commands(rec, segment_pattern)
        producer = [
            shutil.which("ffmpeg"), "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "matroska", "pipe:1",
        ]
        return producer, ffmpeg_cmd


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not available")
def test_user_stop_keeps_and_queues_partial_recording(env) -> None:
    settings = env
    sid = _add_streamer(settings, live=1)
    queued: list[int] = []

    async def scenario() -> None:
        rec = _TestPatternRecorder(settings, on_chunk_queued=queued.append)
        assert await rec.start_recording({"streamer_id": sid, "streamer_name": "Lacy", "platform": "twitch"})
        await asyncio.sleep(5)
        assert await rec.stop_recording(sid)

    asyncio.run(scenario())
    chunks = _rows(settings, "SELECT * FROM recording_chunks")
    assert len(chunks) == 1 and chunks[0]["status"] == "queued", [dict(c) for c in chunks]
    assert queued == [chunks[0]["id"]]
    session = _rows(settings, "SELECT * FROM stream_sessions")[0]
    assert session["status"] == "completed" and session["end_reason"] == "stopped_by_user"


# --- stop hold -----------------------------------------------------------------------


class _AlwaysLive:
    def __init__(self) -> None:
        self.live = True

    async def check_channels(self, logins):
        return {logins[0]: {"external_stream_id": "1", "title": "x", "stream_url": "u"}} if self.live else {}


def test_manual_stop_holds_until_the_stream_goes_offline(env) -> None:
    settings = env
    sid = _add_streamer(settings, live=1, held=1)
    started: list = []
    twitch = _AlwaysLive()

    async def scenario() -> None:
        wm = WatcherManager(
            settings, on_online=started.append, twitch_watcher=twitch, is_recording=lambda _sid: False
        )
        await wm.poll_once()
        assert started == [], "a held streamer must not restart while still live"
        twitch.live = False
        await wm.poll_once()
        assert _rows(settings, "SELECT hold_until_offline FROM streamers")[0][0] == 0
        twitch.live = True
        await wm.poll_once()
        assert len(started) == 1 and started[0].streamer_id == sid

    asyncio.run(scenario())


# --- learning ------------------------------------------------------------------------


def test_desired_ratings_from_fate_and_performance() -> None:
    rows = [
        {"id": 1, "status": "kept_local", "engaged_views": None},
        {"id": 2, "status": "posted", "engaged_views": None},
        {"id": 3, "status": "notified", "engaged_views": None},
    ]
    assert desired_ratings(rows) == {1: (2, "rejected"), 2: (4, "posted")}
    measured = [{"id": i, "status": "posted", "engaged_views": v} for i, v in enumerate([10, 500, 90], start=1)]
    assert desired_ratings(measured) == {1: (3, "performance"), 3: (4, "performance"), 2: (5, "performance")}


def test_learning_sync_records_only_changes(env) -> None:
    settings = env
    sid = _add_streamer(settings)
    now = utcnow_iso()
    conn = sqlite3.connect(settings.db_path)
    for cid, status in ((1, "posted"), (2, "kept_local"), (3, "notified")):
        conn.execute(
            """INSERT INTO clip_drafts (id, streamer_id, file_path, status, features_json, created_at, updated_at)
               VALUES (?, ?, 'x.mp4', ?, ?, ?, ?)""",
            (cid, sid, status, json.dumps({"loud": 0.5, "llm": 0.7}), now, now),
        )
    conn.commit()
    conn.close()

    sent: list[dict] = []

    def fake_cli(payload):
        if payload.get("mode") == "record":
            sent.extend(payload["ratings"])
            return {"recorded": len(payload["ratings"]), "skipped": []}
        return {"ratings": len(sent)}

    svc = LearningService(settings)
    svc._run_cli = fake_cli  # type: ignore[method-assign]
    assert asyncio.run(svc.sync())["recorded"] == 2
    assert {(r["file"], r["rating"]) for r in sent} == {("clip1", 4), ("clip2", 2)}
    assert asyncio.run(svc.sync()) == {"recorded": 0}
    ratings = {r["id"]: (r["rating"], r["rating_source"]) for r in _rows(settings, "SELECT * FROM clip_drafts")}
    assert ratings == {1: (4, "posted"), 2: (2, "rejected"), 3: (None, None)}


# --- API -----------------------------------------------------------------------------


def _client(settings: Settings, monkeypatch) -> TestClient:
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


def test_clip_settings_count_and_hashtags_are_cleaned(env, monkeypatch) -> None:
    with _client(env, monkeypatch) as client:
        sid = client.post(
            "/api/streamers",
            json={"name": "Lacy", "platform": "twitch", "channel_url": "https://twitch.tv/lacy"},
        ).json()["id"]
        res = client.patch(
            f"/api/streamers/{sid}",
            json={"clip_opts": {"clip_count": 25, "hashtags": "gta6, #Lacy  #lacy shorts!"}},
        )
        assert res.status_code == 200
        opts = res.json()["clip_opts"]
        assert opts["clip_count"] == 10
        assert opts["hashtags"] == ["#gta6", "#Lacy", "#shorts"]
        bad = client.patch(f"/api/streamers/{sid}", json={"clip_opts": {"min_len": 90, "max_len": 30}})
        assert bad.status_code == 422


def test_unhandled_errors_reach_the_browser_as_json_with_cors(env, monkeypatch) -> None:
    class Broken:
        async def summary(self):
            raise RuntimeError("database is locked")

    with _client(env, monkeypatch) as client:
        client.app.state.learning = Broken()
        res = client.get("/api/system/learning", headers={"Origin": "http://localhost:3000"})
        assert res.status_code == 500
        assert res.json() == {"detail": "RuntimeError: database is locked"}
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_mojibake_titles_are_repaired() -> None:
    assert repair_text("ðŸš˜ DAY 4") == "\U0001f698 DAY 4"
    assert repair_text("ðŸŒ´ï¸\udc8f GTA 6") == "\U0001f334️ GTA 6"
    assert repair_text("plain title") == "plain title"
