import asyncio
from datetime import datetime, timezone
import json
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
from app.services.neon_client import NeonClippingClient
from app.services.processing_queue import ProcessingQueueWorker
from app.storage import draft_clip_filename, raw_chunk_filename


def _make_synthetic_mp4(path: Path, duration_sec: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x568:rate=15",
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
    assert proc.returncode == 0, f"Synthetic mp4 creation failed: {proc.stderr}"
    return path


def _seed_streamer_session_chunk(
    db_path: Path,
    raw_dir: Path,
    *,
    slug: str = "tarik",
    name: str = "Tarik",
    chunk_index: int = 1,
    status: str = "queued",
    merged_from_chunk_id: int | None = None,
    started_at: datetime | None = None,
) -> tuple[int, int, int, Path]:
    dt = started_at or datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    dt_iso = dt.isoformat()
    chunk_file = raw_dir / raw_chunk_filename(slug, dt, chunk_index)
    chunk_file.parent.mkdir(parents=True, exist_ok=True)
    chunk_file.write_bytes(b"fake-raw-chunk-mp4")

    conn = sqlite3.connect(db_path)
    try:
        existing_s = conn.execute("SELECT id FROM streamers WHERE slug = ?", (slug,)).fetchone()
        if existing_s:
            streamer_id = int(existing_s[0])
        else:
            s_cur = conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES (?, ?, 'twitch', ?, ?, 1, ?, 1, ?, ?)
                """,
                (
                    name,
                    slug,
                    f"https://twitch.tv/{slug}",
                    slug,
                    json.dumps(
                        {
                            "min_len": 25,
                            "max_len": 55,
                            "cam_auto": True,
                            "layout": "stack",
                            "fallback_layout": "blur",
                            "caption_style": "hormozi",
                            "language": "en",
                        }
                    ),
                    dt_iso,
                    dt_iso,
                ),
            )
            streamer_id = int(s_cur.lastrowid)

        existing_sess = conn.execute(
            "SELECT id FROM stream_sessions WHERE streamer_id = ?", (streamer_id,)
        ).fetchone()
        if existing_sess:
            session_id = int(existing_sess[0])
        else:
            sess_cur = conn.execute(
                """
                INSERT INTO stream_sessions (
                    streamer_id, external_stream_id, title, started_at, status, created_at
                )
                VALUES (?, 'ext-1', 'Radiant Grind', ?, 'recording', ?)
                """,
                (streamer_id, dt_iso, dt_iso),
            )
            session_id = int(sess_cur.lastrowid)

        c_cur = conn.execute(
            """
            INSERT INTO recording_chunks (
                session_id, streamer_id, chunk_index, file_path, duration_sec,
                merged_with_prev, merged_from_chunk_id, status, started_at, created_at
            )
            VALUES (?, ?, ?, ?, 60.0, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                streamer_id,
                chunk_index,
                str(chunk_file),
                1 if merged_from_chunk_id is not None else 0,
                merged_from_chunk_id,
                status,
                dt_iso,
                dt_iso,
            ),
        )
        chunk_id = int(c_cur.lastrowid)
        conn.commit()
        return streamer_id, session_id, chunk_id, chunk_file
    finally:
        conn.close()


class FakeNeonClient:
    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_with = fail_with

    async def arefine_and_export_with_turbo(
        self,
        chunk_path,
        output_dir=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "chunk_path": str(chunk_path),
                "output_dir": str(output_dir) if output_dir else None,
                **kwargs,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        clips = []
        for idx, (s, e) in enumerate(((10.0, 42.0), (120.0, 155.0), (240.0, 275.0)), start=1):
            mp4 = _make_synthetic_mp4(out_dir / f"staged_{idx}.mp4", duration_sec=2.0)
            srt = out_dir / f"staged_{idx}.srt"
            srt.write_text(f"1\n00:00:00,000 --> 00:00:02,000\nClip {idx}\n", encoding="utf-8")
            clips.append(
                {
                    "index": idx,
                    "start": s,
                    "end": e,
                    "duration": e - s,
                    "score": 90 + idx,
                    "title": f"Neon Title {idx}",
                    "hook": f"Neon Hook {idx}",
                    "transcript": f"Transcript text for clip {idx}",
                    "output_mp4": str(mp4),
                    "output_srt": str(srt),
                }
            )
        return {"ok": True, "clips": clips}


class FakeCopywriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate_youtube_copy(
        self,
        *,
        transcript: str,
        neon_title: str = "",
        neon_hook: str = "",
        streamer_name: str = "",
        stream_title: str = "",
        platform: str = "",
    ):
        self.calls.append(
            {
                "transcript": transcript,
                "neon_title": neon_title,
                "neon_hook": neon_hook,
                "streamer_name": streamer_name,
                "stream_title": stream_title,
                "platform": platform,
            }
        )
        return {
            "title": f"{streamer_name}: {neon_title}",
            "description": f"Watch {streamer_name} pull off {neon_hook} live on {platform}.",
            "tags": ["shorts", streamer_name.lower(), "gaming"],
            "source": "llm",
        }


class FakeGoogleDrive:
    def __init__(self, *, mode: str = "ok") -> None:
        self.mode = mode
        self.uploads: list[dict] = []
        self.deletes: list[str] = []

    async def upload_clip(
        self,
        db,
        file_path,
        *,
        streamer_name: str,
        clip_date: str,
        display_name: str | None = None,
        account_id: int | None = None,
        share_anyone: bool = True,
    ):
        self.uploads.append(
            {
                "file_path": str(file_path),
                "streamer_name": streamer_name,
                "clip_date": clip_date,
                "display_name": display_name,
                "share_anyone": share_anyone,
            }
        )
        if self.mode == "none":
            return None
        if self.mode == "error":
            raise RuntimeError("Drive API unavailable")
        idx = len(self.uploads)
        return {
            "drive_file_id": f"drv-file-{idx}",
            "drive_url": f"https://drive.google.com/file/d/drv-file-{idx}/view",
        }

    async def delete_file(self, db, file_id: str, account_id: int | None = None) -> bool:
        self.deletes.append(file_id)
        return True


@pytest.fixture()
def queue_env():
    with tempfile.TemporaryDirectory(prefix="clip_phase3_") as tmp:
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
            llm_base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            llm_model="deepseek-v4-1-flash-261017",
            llm_api_key="test-llm-key",
            token_encryption_key=generate_fernet_key(),
            enable_background_workers=False,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, Path(tmp)


def test_neon_client_merges_extra_env(monkeypatch: pytest.MonkeyPatch, queue_env) -> None:
    settings, tmp_root = queue_env
    engine_dir = tmp_root / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    (engine_dir / "pipeline_runner.py").write_text("print('{}')\n", encoding="utf-8")
    fake_py = tmp_root / "python.exe"
    fake_py.write_bytes(b"")
    chunk = tmp_root / "chunk.mp4"
    chunk.write_bytes(b"mp4")

    captured_env: dict[str, str] = {}

    class FakePopen:
        pid = 0
        returncode = 0

        def __init__(self, cmd, cwd=None, env=None, **kwargs):
            captured_env.update(env or {})

        def communicate(self, input=None):
            return '{"ok": true, "clips": []}\n', ""

        def poll(self):
            return self.returncode

    monkeypatch.setattr("app.services.neon_client.subprocess.Popen", FakePopen)

    client = NeonClippingClient(
        base_url=settings.neonclipping_url,
        data_dir=settings.neonclipping_data_dir,
        engine_dir=engine_dir,
        python_executable=fake_py,
    )
    res = asyncio.run(
        client.arefine_and_export_with_turbo(
            chunk,
            extra_env={
                "NEONCLIPPING_LLM_BASE_URL": "https://ark.ap-southeast.bytepluses.com/api/v3",
                "NEONCLIPPING_LLM_MODEL": "deepseek-v4-1-flash-261017",
                "NEONCLIPPING_LLM_KEY": "secret-key",
            },
        )
    )
    assert res["ok"] is True
    assert captured_env["NEONCLIPPING_LLM_BASE_URL"] == "https://ark.ap-southeast.bytepluses.com/api/v3"
    assert captured_env["NEONCLIPPING_LLM_MODEL"] == "deepseek-v4-1-flash-261017"
    assert captured_env["NEONCLIPPING_LLM_KEY"] == "secret-key"
    assert captured_env["NEONCLIPPING_NO_AUTOUPDATE"] == "1"


def test_process_chunk_canonical_drafts_thumbnails_and_tail_dedup(queue_env) -> None:
    settings, _ = queue_env
    dt = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    _, _, chunk1_id, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="tarik",
        name="Tarik",
        chunk_index=1,
        started_at=dt,
    )

    ready_events: list[tuple[int, list[int]]] = []

    async def on_ready(cid: int, clip_ids: list[int]) -> None:
        ready_events.append((cid, clip_ids))

    neon = FakeNeonClient()
    copywriter = FakeCopywriter()
    drive = FakeGoogleDrive(mode="ok")
    worker = ProcessingQueueWorker(
        settings,
        neon_client=neon,
        copywriter=copywriter,
        google_drive=drive,
        on_chunk_drafts_ready=on_ready,
    )

    res = asyncio.run(worker.process_chunk(chunk1_id))
    assert res["ok"] is True
    assert res["status"] == "processed"
    assert res["count"] == 3
    assert len(ready_events) == 1
    assert ready_events[0][0] == chunk1_id
    assert len(ready_events[0][1]) == 3

    # Verify extra_env and clip_opts forwarded
    assert len(neon.calls) == 1
    assert neon.calls[0]["exclude_intervals"] is None
    assert neon.calls[0]["min_len"] == 25.0
    assert neon.calls[0]["max_len"] == 55.0
    assert neon.calls[0]["language"] == "en"
    assert neon.calls[0]["extra_env"] == {
        "NEONCLIPPING_LLM_BASE_URL": "https://ark.ap-southeast.bytepluses.com/api/v3",
        "NEONCLIPPING_LLM_MODEL": "deepseek-v4-1-flash-261017",
        "NEONCLIPPING_LLM_KEY": "test-llm-key",
    }

    # Verify canonical _draft_1..3.{mp4,srt,jpg} exist in drafts_dir
    drafts_dir = Path(settings.drafts_dir)
    for dnum in (1, 2, 3):
        mp4_p = drafts_dir / draft_clip_filename("tarik", dt, 1, dnum, ext="mp4")
        srt_p = drafts_dir / draft_clip_filename("tarik", dt, 1, dnum, ext="srt")
        jpg_p = drafts_dir / draft_clip_filename("tarik", dt, 1, dnum, ext="jpg")
        assert mp4_p.is_file()
        assert srt_p.is_file()
        assert jpg_p.is_file() and jpg_p.stat().st_size > 0

    # Verify DB rows
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        chunk_row = conn.execute(
            "SELECT status, last_error FROM recording_chunks WHERE id = ?",
            (chunk1_id,),
        ).fetchone()
        assert chunk_row["status"] == "processed"
        assert chunk_row["last_error"] is None

        draft_rows = conn.execute(
            "SELECT * FROM clip_drafts WHERE chunk_id = ? ORDER BY draft_number ASC",
            (chunk1_id,),
        ).fetchall()
        assert len(draft_rows) == 3
        assert draft_rows[0]["title"] == "Tarik: Neon Title 1"
        assert draft_rows[0]["drive_file_id"] == "drv-file-1"
        assert draft_rows[0]["status"] == "draft"
        assert draft_rows[0]["start_sec"] == 10.0
        assert draft_rows[0]["end_sec"] == 42.0
    finally:
        conn.close()

    # Verify tail-merged chunk passes exclude_intervals from chunk 1 drafts
    _, _, chunk2_id, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="tarik",
        name="Tarik",
        chunk_index=2,
        merged_from_chunk_id=chunk1_id,
        started_at=dt,
    )
    asyncio.run(worker.process_chunk(chunk2_id))
    assert len(neon.calls) == 2
    assert neon.calls[1]["exclude_intervals"] == [
        (10.0, 42.0),
        (120.0, 155.0),
        (240.0, 275.0),
    ]


def test_process_chunk_drive_error_non_fatal_and_coding_url_blocked(queue_env) -> None:
    settings, _ = queue_env
    settings.llm_base_url = "https://ark.ap-southeast.bytepluses.com/api/coding/v3"

    _, _, chunk_id, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="caedrel",
        name="Caedrel",
    )

    neon = FakeNeonClient()
    copywriter = FakeCopywriter()
    drive = FakeGoogleDrive(mode="error")
    worker = ProcessingQueueWorker(
        settings,
        neon_client=neon,
        copywriter=copywriter,
        google_drive=drive,
    )

    res = asyncio.run(worker.process_chunk(chunk_id))
    assert res["ok"] is True
    assert neon.calls[0]["extra_env"] is None

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT drive_file_id, drive_url FROM clip_drafts WHERE chunk_id = ?", (chunk_id,)).fetchall()
        assert len(rows) == 3
        for r in rows:
            assert r["drive_file_id"] is None
            assert r["drive_url"] is None
        c_row = conn.execute("SELECT status FROM recording_chunks WHERE id = ?", (chunk_id,)).fetchone()
        assert c_row["status"] == "processed"
    finally:
        conn.close()


def test_process_chunk_failure_records_last_error_and_cleans_staging(queue_env) -> None:
    settings, _ = queue_env
    _, _, chunk_id, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="failer",
        name="Failer",
    )

    worker = ProcessingQueueWorker(
        settings,
        neon_client=FakeNeonClient(fail_with=RuntimeError("CUDA out of memory")),
        copywriter=FakeCopywriter(),
        google_drive=FakeGoogleDrive(),
    )

    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        asyncio.run(worker.process_chunk(chunk_id))

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, last_error FROM recording_chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        assert row["status"] == "failed"
        assert "CUDA out of memory" in (row["last_error"] or "")
    finally:
        conn.close()

    staging_leftovers = list(Path(settings.neonclipping_data_dir).glob("chunk_*"))
    assert staging_leftovers == []


def test_worker_start_recovers_stale_processing_and_dedups_enqueue(queue_env) -> None:
    settings, _ = queue_env
    _, _, c1, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="recov",
        name="Recov",
        chunk_index=1,
        status="processing",
    )
    _, _, c2, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="recov",
        name="Recov",
        chunk_index=2,
        status="queued",
    )

    async def scenario() -> None:
        neon = FakeNeonClient()
        worker = ProcessingQueueWorker(
            settings,
            neon_client=neon,
            copywriter=FakeCopywriter(),
            google_drive=FakeGoogleDrive(mode="none"),
        )
        worker.pause()
        await worker.start()
        try:
            st = worker.status()
            assert st["paused"] is True
            assert st["queued_count"] == 2
            assert worker.enqueue_chunk(c1) is False
            assert worker.enqueue_chunk(c2) is False

            worker.resume()
            for _ in range(100):
                conn = sqlite3.connect(settings.db_path)
                try:
                    done_count = conn.execute(
                        "SELECT COUNT(*) FROM recording_chunks WHERE id IN (?, ?) AND status = 'processed'",
                        (c1, c2),
                    ).fetchone()[0]
                finally:
                    conn.close()
                if done_count == 2 and worker.status()["running_chunk_id"] is None:
                    break
                await asyncio.sleep(0.05)
            assert len(neon.calls) == 2
            assert worker.status()["queued_count"] == 0
        finally:
            await worker.stop()

    asyncio.run(scenario())


def test_clips_and_queue_api_endpoints(queue_env, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _ = queue_env
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    dt = datetime(2026, 11, 22, 14, 0, 0, tzinfo=timezone.utc)
    streamer_id, session_id, chunk_id, _ = _seed_streamer_session_chunk(
        Path(settings.db_path),
        Path(settings.raw_recordings_dir),
        slug="caedrel",
        name="Caedrel",
        chunk_index=1,
        status="failed",
        started_at=dt,
    )

    mp4_path = Path(settings.drafts_dir) / draft_clip_filename("caedrel", dt, 1, 1, ext="mp4")
    srt_path = Path(settings.drafts_dir) / draft_clip_filename("caedrel", dt, 1, 1, ext="srt")
    jpg_path = Path(settings.drafts_dir) / draft_clip_filename("caedrel", dt, 1, 1, ext="jpg")
    _make_synthetic_mp4(mp4_path, duration_sec=1.5)
    srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    jpg_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    now = utcnow_iso()
    conn = sqlite3.connect(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO clip_drafts (
                streamer_id, session_id, chunk_id, draft_number,
                title, description, tags_json, file_path, thumbnail_path,
                start_sec, end_sec, duration_sec, score, status, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, 'Initial Title', 'Initial one sentence.', ?, ?, ?, 1.0, 2.5, 1.5, 88.0, 'draft', ?, ?)
            """,
            (streamer_id, session_id, chunk_id, json.dumps(["lol"]), str(mp4_path), str(jpg_path), now, now),
        )
        clip_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    with TestClient(app) as client:
        fake_drive = FakeGoogleDrive(mode="none")
        app.state.google_drive = fake_drive

        # GET /api/clips includes streamer_name
        list_resp = client.get("/api/clips")
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["streamer_name"] == "Caedrel"

        # GET /api/clips/{id}
        one_resp = client.get(f"/api/clips/{clip_id}")
        assert one_resp.status_code == 200
        assert one_resp.json()["title"] == "Initial Title"
        assert client.get("/api/clips/99999").status_code == 404

        # PATCH /api/clips/{id}
        patch_resp = client.patch(
            f"/api/clips/{clip_id}",
            json={"title": "Edited Title", "description": "Edited sentence.", "tags": ["shorts", "lec"]},
        )
        assert patch_resp.status_code == 200
        patched = patch_resp.json()
        assert patched["title"] == "Edited Title"
        assert patched["description"] == "Edited sentence."
        assert patched["tags"] == ["shorts", "lec"]

        # GET /api/clips/{id}/video & /thumbnail
        vid_resp = client.get(f"/api/clips/{clip_id}/video")
        assert vid_resp.status_code == 200
        assert vid_resp.headers["content-type"].startswith("video/mp4")

        thumb_resp = client.get(f"/api/clips/{clip_id}/thumbnail")
        assert thumb_resp.status_code == 200
        assert thumb_resp.headers["content-type"].startswith("image/jpeg")

        # POST /api/clips/{id}/upload-drive -> 400 when no Google Drive account connected
        up_400 = client.post(f"/api/clips/{clip_id}/upload-drive")
        assert up_400.status_code == 400

        # Switch drive mode to ok -> 200
        fake_drive.mode = "ok"
        up_200 = client.post(f"/api/clips/{clip_id}/upload-drive")
        assert up_200.status_code == 200
        up_body = up_200.json()
        assert up_body["drive_file_id"] == "drv-file-2"
        assert up_body["drive_url"] == "https://drive.google.com/file/d/drv-file-2/view"

        # Queue endpoints
        q_resp = client.get("/api/system/queue")
        assert q_resp.status_code == 200
        q_body = q_resp.json()
        assert q_body["paused"] is False
        assert q_body["running_chunk_id"] is None
        assert len(q_body["chunks"]) == 1
        assert q_body["chunks"][0]["streamer_name"] == "Caedrel"

        assert client.post("/api/system/queue/pause").json() == {"paused": True}
        assert client.get("/api/system/queue").json()["paused"] is True
        assert client.post("/api/system/queue/resume").json() == {"paused": False}

        assert client.post("/api/system/queue/process/99999").status_code == 404
        proc_resp = client.post(f"/api/system/queue/process/{chunk_id}")
        assert proc_resp.status_code == 200
        assert proc_resp.json() == {"ok": True, "chunk_id": chunk_id, "status": "queued"}
        assert client.get("/api/system/queue").json()["queued_count"] >= 1

        # DELETE /api/clips/{id}?delete_drive=true deletes mp4, srt, jpg, and calls Drive delete
        del_resp = client.delete(f"/api/clips/{clip_id}?delete_drive=true")
        assert del_resp.status_code == 200
        assert del_resp.json() == {"ok": True, "deleted_local": True, "deleted_drive": True}
        assert not mp4_path.exists()
        assert not srt_path.exists()
        assert not jpg_path.exists()
        assert fake_drive.deletes == ["drv-file-2"]
