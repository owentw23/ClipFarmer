import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
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
from app.services.recorder import (
    RecorderManager,
    concat_chunks_lossless,
    finalize_stream_end,
    probe_duration,
    process_full_chunk,
    remux_to_mp4,
)
from app.services.retention_gc import RetentionGC
from app.storage import raw_chunk_filename


def _make_synthetic_mkv(path: Path, duration_sec: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(duration_sec),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Synthetic mkv creation failed: {proc.stderr}"
    return path


def _seed_streamer_and_session(db_path: Path, slug: str = "tarik") -> tuple[int, int]:
    now = utcnow_iso()
    conn = sqlite3.connect(db_path)
    try:
        s_cur = conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES (?, ?, 'twitch', ?, ?, 1, '{}', 1, ?, ?)
            """,
            (slug.title(), slug, f"https://twitch.tv/{slug}", slug, now, now),
        )
        streamer_id = int(s_cur.lastrowid)
        sess_cur = conn.execute(
            """
            INSERT INTO stream_sessions (
                streamer_id, external_stream_id, title, started_at, status, created_at
            )
            VALUES (?, 'stream-1', 'Ranked Grind', ?, 'recording', ?)
            """,
            (streamer_id, now, now),
        )
        session_id = int(sess_cur.lastrowid)
        conn.commit()
        return streamer_id, session_id
    finally:
        conn.close()


@pytest.fixture()
def recorder_env():
    with tempfile.TemporaryDirectory(prefix="clip_phase2_rec_") as tmp:
        root = Path(tmp) / "Clips_Store"
        raw_dir = root / "raw recordings"
        drafts_dir = root / "drafts"
        neon_dir = root / "neon_tmp"
        db_file = root / "db" / "pipeline.sqlite3"
        raw_dir.mkdir(parents=True, exist_ok=True)
        drafts_dir.mkdir(parents=True, exist_ok=True)
        neon_dir.mkdir(parents=True, exist_ok=True)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        settings = Settings(
            clips_store_root=str(root),
            raw_recordings_dir=str(raw_dir),
            drafts_dir=str(drafts_dir),
            neonclipping_data_dir=str(neon_dir),
            db_path=str(db_file),
            token_encryption_key=generate_fernet_key(),
            enable_background_workers=False,
            chunk_duration_seconds=3,
            chunk_retention_seconds=7200,
            gc_interval_seconds=60,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, Path(tmp)


def test_ffmpeg_helpers_probe_remux_and_concat(recorder_env) -> None:
    settings, tmp_root = recorder_env
    mkv_a = _make_synthetic_mkv(tmp_root / "src" / "a.mkv", 3.0)
    mkv_b = _make_synthetic_mkv(tmp_root / "src" / "b.mkv", 2.0)

    mp4_a = Path(settings.raw_recordings_dir) / "a.mp4"
    asyncio.run(remux_to_mp4(mkv_a, mp4_a))
    assert mp4_a.is_file()
    dur_a = asyncio.run(probe_duration(mp4_a))
    assert 2.7 <= dur_a <= 3.5

    merged = Path(settings.raw_recordings_dir) / "a_merged.mp4"
    asyncio.run(concat_chunks_lossless(mp4_a, mkv_b, merged))
    assert merged.is_file()
    dur_merged = asyncio.run(probe_duration(merged))
    assert 4.6 <= dur_merged <= 5.5


def test_standalone_short_stream_discarded_and_deleted(recorder_env) -> None:
    settings, tmp_root = recorder_env
    streamer_id, session_id = _seed_streamer_and_session(Path(settings.db_path), slug="shorty")
    short_mkv = _make_synthetic_mkv(tmp_root / "raw_tmp" / "short_chunk01.mkv", 1.5)
    queued_ids: list[int] = []

    async def on_queued(cid: int) -> None:
        queued_ids.append(cid)

    start_dt = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
    result = asyncio.run(
        finalize_stream_end(
            db_path=settings.db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            streamer_slug="shorty",
            tail_mkv_path=short_mkv,
            chunk_index=1,
            chunk_started_at=start_dt,
            settings=settings,
            last_full_chunk=None,
            on_chunk_queued=on_queued,
        )
    )

    assert result["action"] == "discarded_short"
    assert not short_mkv.exists(), "Short partial file must be deleted from disk"
    assert queued_ids == [], "Standalone short stream must not queue any clip"

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM recording_chunks WHERE id = ?",
            (result["chunk_id"],),
        ).fetchone()
        assert row is not None
        assert row["status"] == "discarded_short"
        assert row["delete_after"] is None
    finally:
        conn.close()


def test_full_chunk_remux_and_partial_tail_lossless_merge_and_retention_gc(recorder_env) -> None:
    settings, tmp_root = recorder_env
    streamer_id, session_id = _seed_streamer_and_session(Path(settings.db_path), slug="tarik")
    queued_ids: list[int] = []

    async def on_queued(cid: int) -> None:
        queued_ids.append(cid)

    t0 = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    chunk1_mkv = _make_synthetic_mkv(tmp_root / "capture" / "chunk01.mkv", 3.0)
    full_res = asyncio.run(
        process_full_chunk(
            db_path=settings.db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            streamer_slug="tarik",
            src_mkv=chunk1_mkv,
            chunk_index=1,
            chunk_started_at=t0,
            settings=settings,
            on_chunk_queued=on_queued,
        )
    )

    expected_chunk1_path = Path(settings.raw_recordings_dir) / raw_chunk_filename("tarik", t0, 1)
    assert full_res["action"] == "queued_full"
    assert Path(full_res["file_path"]) == expected_chunk1_path
    assert expected_chunk1_path.is_file()
    assert not chunk1_mkv.exists()
    assert queued_ids == [full_res["chunk_id"]]

    expected_delete_after = (t0 + timedelta(seconds=settings.chunk_retention_seconds)).isoformat()
    assert full_res["delete_after"] == expected_delete_after

    # Tail chunk (< 3s) after Chunk 1 merges losslessly into <chunk1_stem>_merged.mp4
    tail_mkv = _make_synthetic_mkv(tmp_root / "capture" / "chunk02.mkv", 2.0)
    t1 = t0 + timedelta(seconds=3)
    merge_res = asyncio.run(
        finalize_stream_end(
            db_path=settings.db_path,
            session_id=session_id,
            streamer_id=streamer_id,
            streamer_slug="tarik",
            tail_mkv_path=tail_mkv,
            chunk_index=2,
            chunk_started_at=t1,
            settings=settings,
            on_chunk_queued=on_queued,
        )
    )

    assert merge_res["action"] == "merged_tail"
    assert not tail_mkv.exists(), "Raw tail file must be deleted immediately after merge"
    expected_merged_path = expected_chunk1_path.with_name(f"{expected_chunk1_path.stem}_merged.mp4")
    assert Path(merge_res["file_path"]) == expected_merged_path
    assert expected_merged_path.is_file()
    assert 4.6 <= merge_res["duration_sec"] <= 5.5
    assert queued_ids == [full_res["chunk_id"], merge_res["chunk_id"]]

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        merged_row = conn.execute(
            "SELECT * FROM recording_chunks WHERE id = ?",
            (merge_res["chunk_id"],),
        ).fetchone()
        assert merged_row["merged_with_prev"] == 1
        assert merged_row["merged_from_chunk_id"] == full_res["chunk_id"]
        assert merged_row["status"] == "queued"
    finally:
        conn.close()

    # RetentionGC safety-lock verification
    gc = RetentionGC(settings)
    past_retention = max(t0, datetime.now(timezone.utc)) + timedelta(
        seconds=settings.chunk_retention_seconds + 60
    )

    # 1) Chunk 1 is still status='queued' -> refused!
    res_queued = asyncio.run(gc.run_once(now=past_retention))
    assert res_queued["deleted_chunk_ids"] == []
    assert res_queued["skipped_locked"] == 2
    assert expected_chunk1_path.is_file()

    # 2) Chunk 1 becomes 'processed', but merged child is still 'queued'/'processing' -> Chunk 1 STILL refused!
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE recording_chunks SET status = 'processed' WHERE id = ?",
            (full_res["chunk_id"],),
        )
        conn.execute(
            "UPDATE recording_chunks SET status = 'processing' WHERE id = ?",
            (merge_res["chunk_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    res_child_locked = asyncio.run(gc.run_once(now=past_retention))
    assert res_child_locked["deleted_chunk_ids"] == []
    assert res_child_locked["skipped_locked"] == 2
    assert expected_chunk1_path.is_file()

    # 3) Merged child becomes 'processed', and we run at t0 + 120m + 1s (before merged child's own delete_after if later,
    #    or check exact behavior at past_retention):
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE recording_chunks SET status = 'processed', delete_after = ? WHERE id = ?",
            ((past_retention + timedelta(minutes=30)).isoformat(), merge_res["chunk_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    res_unlocked = asyncio.run(gc.run_once(now=past_retention))
    assert res_unlocked == {"deleted_chunk_ids": [full_res["chunk_id"]], "skipped_locked": 0}
    assert not expected_chunk1_path.exists()
    assert expected_merged_path.is_file()

    conn = sqlite3.connect(settings.db_path)
    try:
        status_row = conn.execute(
            "SELECT status FROM recording_chunks WHERE id = ?",
            (full_res["chunk_id"],),
        ).fetchone()
        assert status_row[0] == "deleted"
    finally:
        conn.close()


def test_system_endpoints_end_to_end(recorder_env, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _ = recorder_env
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    class FakeWatcherManager:
        def __init__(self, streamer_id_holder: dict[str, int]) -> None:
            self.holder = streamer_id_holder

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def check_streamer(self, streamer_id: int, db_path=None):
            if streamer_id != self.holder["id"]:
                raise LookupError("Streamer not found")
            return {
                "streamer_id": streamer_id,
                "is_live": True,
                "stream": {
                    "streamer_id": streamer_id,
                    "streamer_name": "Caedrel",
                    "streamer_slug": "caedrel",
                    "platform": "twitch",
                    "channel_ref": "caedrel",
                    "stream_url": "https://www.twitch.tv/caedrel",
                    "external_stream_id": "live-999",
                    "title": "LEC Costream",
                },
                "checked_at": utcnow_iso(),
            }

    class FakeRecorderManager(RecorderManager):
        def __init__(self, cfg: Settings) -> None:
            super().__init__(cfg)
            self.fake_running: set[int] = set()

        def is_recording(self, streamer_id: int) -> bool:
            return streamer_id in self.fake_running

        async def start_recording(self, stream_info) -> bool:
            sid = int(stream_info["streamer_id"])
            if sid in self.fake_running:
                return False
            self.fake_running.add(sid)
            return True

        async def stop_recording(self, streamer_id: int) -> bool:
            if streamer_id in self.fake_running:
                self.fake_running.remove(streamer_id)
                return True
            return False

        def active_recordings(self):
            return [
                {
                    "streamer_id": sid,
                    "streamer_name": "Caedrel",
                    "streamer_slug": "caedrel",
                    "platform": "twitch",
                    "stream_url": "https://www.twitch.tv/caedrel",
                    "external_stream_id": "live-999",
                    "title": "LEC Costream",
                    "session_id": 1,
                    "started_at": utcnow_iso(),
                    "chunks_completed": 0,
                }
                for sid in sorted(self.fake_running)
            ]

    with TestClient(app) as client:
        s_resp = client.post(
            "/api/streamers",
            json={
                "name": "Caedrel",
                "platform": "twitch",
                "channel_url": "https://www.twitch.tv/caedrel",
            },
        )
        assert s_resp.status_code == 201
        streamer_id = s_resp.json()["id"]

        fake_recorder = FakeRecorderManager(settings)
        app.state.recorder = fake_recorder
        app.state.watcher_manager = FakeWatcherManager({"id": streamer_id})
        app.state.retention_gc = RetentionGC(settings)

        # Check live -> starts recording
        chk_resp = client.post(f"/api/streamers/{streamer_id}/check")
        assert chk_resp.status_code == 200
        chk_data = chk_resp.json()
        assert chk_data["is_live"] is True
        assert chk_data["recording_started"] is True

        # Check again while already recording -> recording_started False
        chk_again = client.post(f"/api/streamers/{streamer_id}/check")
        assert chk_again.status_code == 200
        assert chk_again.json()["recording_started"] is False

        # Missing streamer -> 404
        assert client.post("/api/streamers/99999/check").status_code == 404
        assert client.post("/api/streamers/99999/stop").status_code == 404

        # Seed one expired processed chunk for GC + /api/system/recordings check
        expired_file = Path(settings.raw_recordings_dir) / "caedrel_2026-11-22_010000_chunk01.mp4"
        expired_file.write_bytes(b"expired-video")
        past_iso = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        conn = sqlite3.connect(settings.db_path)
        try:
            sess = conn.execute(
                "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'completed', ?)",
                (streamer_id, past_iso, past_iso),
            )
            cur = conn.execute(
                """
                INSERT INTO recording_chunks (
                    session_id, streamer_id, chunk_index, file_path, duration_sec,
                    merged_with_prev, status, started_at, completed_at, delete_after, created_at
                )
                VALUES (?, ?, 1, ?, 3.0, 0, 'processed', ?, ?, ?, ?)
                """,
                (sess.lastrowid, streamer_id, str(expired_file), past_iso, past_iso, past_iso, past_iso),
            )
            expired_chunk_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        rec_resp = client.get("/api/system/recordings")
        assert rec_resp.status_code == 200
        rec_body = rec_resp.json()
        assert len(rec_body["active"]) == 1
        assert rec_body["active"][0]["streamer_id"] == streamer_id
        assert len(rec_body["recent_chunks"]) == 1
        assert rec_body["recent_chunks"][0]["streamer_slug"] == "caedrel"

        # Stop recording
        stop_resp = client.post(f"/api/streamers/{streamer_id}/stop")
        assert stop_resp.status_code == 200
        assert stop_resp.json() == {"ok": True, "streamer_id": streamer_id, "stopped": True, "held": True}

        # Run GC now
        gc_resp = client.post("/api/system/gc/run")
        assert gc_resp.status_code == 200
        assert gc_resp.json() == {"deleted_chunk_ids": [expired_chunk_id], "skipped_locked": 0}
        assert not expired_file.exists()
