import asyncio
from datetime import datetime
import json
from pathlib import Path
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
from app.security import decrypt_token, encrypt_token, generate_fernet_key
from app.storage import (
    draft_clip_filename,
    ensure_storage_dirs,
    raw_chunk_filename,
    slugify_streamer,
)


def test_runtime_clips_store_dirs_exist_on_disk() -> None:
    expected = [
        Path(r"C:\Users\wirya\Documents\Clips_Store\raw recordings"),
        Path(r"C:\Users\wirya\Documents\Clips_Store\drafts"),
        Path(r"C:\Users\wirya\Documents\Clips_Store\neon_tmp"),
        Path(r"C:\Users\wirya\Documents\Clips_Store\db"),
    ]
    for folder in expected:
        assert folder.is_dir(), f"Missing runtime folder: {folder}"


@pytest.fixture()
def work_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="clip_phase1_") as tmp:
        yield Path(tmp)


def test_ensure_storage_dirs_creates_custom_root(work_dir: Path) -> None:
    cfg = Settings(
        clips_store_root=str(work_dir / "store"),
        raw_recordings_dir=str(work_dir / "store" / "raw recordings"),
        drafts_dir=str(work_dir / "store" / "drafts"),
        neonclipping_data_dir=str(work_dir / "store" / "neon_tmp"),
        db_path=str(work_dir / "store" / "db" / "pipeline.sqlite3"),
    )
    paths = ensure_storage_dirs(cfg)
    for key in ("root", "raw_recordings", "drafts", "neon_tmp", "db_dir"):
        assert paths[key].is_dir()


def test_filename_builders_and_slugify() -> None:
    assert slugify_streamer("Tarik (FPS)!") == "tarik-fps"
    assert slugify_streamer("   ") == "streamer"

    dt = datetime(2026, 11, 22, 9, 5, 3)
    assert raw_chunk_filename("Tarik", dt, 1) == "tarik_2026-11-22_090503_chunk01.mp4"
    assert (
        draft_clip_filename("Tarik", dt, 2, 3)
        == "tarik_2026-11-22_090503_chunk02_draft_3.mp4"
    )
    assert (
        draft_clip_filename("Tarik", dt, 2, 3, ext=".mov")
        == "tarik_2026-11-22_090503_chunk02_draft_3.mov"
    )


def test_fernet_encrypt_decrypt_round_trip() -> None:
    key = generate_fernet_key()
    secret = "ya29.test-refresh-token-value"
    encrypted = encrypt_token(secret, key=key)
    assert encrypted != secret
    assert decrypt_token(encrypted, key=key) == secret

    other_key = generate_fernet_key()
    with pytest.raises(ValueError):
        decrypt_token(encrypted, key=other_key)

    with pytest.raises(ValueError):
        encrypt_token(secret, key="")


def test_db_schema_and_wal_mode(work_dir: Path) -> None:
    db_file = work_dir / "test_schema.sqlite3"
    asyncio.run(init_db(db_file))

    conn = sqlite3.connect(db_file)
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(mode).lower() == "wal"

        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {row[0] for row in rows}
        expected_tables = {
            "streamers",
            "youtube_accounts",
            "streamer_targets",
            "stream_sessions",
            "recording_chunks",
            "clip_drafts",
            "publications",
            "metric_snapshots",
        }
        assert expected_tables.issubset(tables)
    finally:
        conn.close()


@pytest.fixture()
def api_client(work_dir: Path, monkeypatch: pytest.MonkeyPatch):
    store_root = work_dir / "Clips_Store"
    raw_dir = store_root / "raw recordings"
    drafts_dir = store_root / "drafts"
    neon_dir = store_root / "neon_tmp"
    db_file = store_root / "db" / "pipeline.sqlite3"

    test_settings = Settings(
        clips_store_root=str(store_root),
        raw_recordings_dir=str(raw_dir),
        drafts_dir=str(drafts_dir),
        neonclipping_data_dir=str(neon_dir),
        db_path=str(db_file),
        llm_api_key="",
        neonclipping_llm_key="",
        token_encryption_key=generate_fernet_key(),
    )

    monkeypatch.setattr("app.main.get_settings", lambda: test_settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: test_settings

    with TestClient(app) as client:
        yield client, test_settings


def test_health_endpoint(api_client) -> None:
    client, _ = api_client
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    checks = body["checks"]
    assert set(checks.keys()) == {"database", "storage", "ffmpeg", "neonclipping", "llm"}
    assert checks["database"]["ok"] is True
    assert checks["storage"]["ok"] is True
    assert isinstance(checks["storage"]["free_gb"], float)
    assert isinstance(checks["ffmpeg"]["ok"], bool)
    assert isinstance(checks["ffmpeg"]["nvenc"], bool)
    assert isinstance(checks["neonclipping"]["ok"], bool)
    assert checks["llm"]["configured"] is False


def test_streamers_crud(api_client) -> None:
    client, _ = api_client

    # Initial empty list
    resp = client.get("/api/streamers")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}

    # Create Twitch streamer with default clip_opts
    create_resp = client.post(
        "/api/streamers",
        json={
            "name": "Tarik",
            "platform": "twitch",
            "channel_url": "https://www.twitch.tv/tarik",
        },
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["slug"] == "tarik"
    assert created["channel_ref"] == "tarik"
    assert created["auto_watch"] is True
    assert created["clip_opts"] == {
        "min_len": 30,
        "max_len": 60,
        "cam_auto": True,
        "layout": "stack",
        "fallback_layout": "blur",
        "caption_style": "hormozi",
        "language": None,
        "clip_count": 3,
        "hashtags": [],
    }
    streamer_id = created["id"]

    # Create YouTube streamer with custom clip_opts override
    yt_resp = client.post(
        "/api/streamers",
        json={
            "name": "Ludwig Live",
            "platform": "youtube",
            "channel_url": "https://www.youtube.com/@ludwig",
            "auto_watch": False,
            "clip_opts": {"min_len": 20, "language": "en"},
        },
    )
    assert yt_resp.status_code == 201
    yt_item = yt_resp.json()
    assert yt_item["slug"] == "ludwig-live"
    assert yt_item["channel_ref"] == "@ludwig"
    assert yt_item["auto_watch"] is False
    assert yt_item["clip_opts"]["min_len"] == 20
    assert yt_item["clip_opts"]["max_len"] == 60
    assert yt_item["clip_opts"]["language"] == "en"

    # List returns both
    list_resp = client.get("/api/streamers")
    assert list_resp.status_code == 200
    assert len(list_resp.json()["items"]) == 2

    # Patch streamer
    patch_resp = client.patch(
        f"/api/streamers/{streamer_id}",
        json={"auto_watch": False, "clip_opts": {"caption_style": "clean"}},
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["auto_watch"] is False
    assert patched["clip_opts"]["caption_style"] == "clean"
    assert patched["clip_opts"]["layout"] == "stack"

    # Delete streamer
    del_resp = client.delete(f"/api/streamers/{streamer_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["ok"] is True

    # Delete missing returns 404
    missing_resp = client.delete(f"/api/streamers/{streamer_id}")
    assert missing_resp.status_code == 404


def test_accounts_clips_and_metrics_endpoints(api_client) -> None:
    client, settings = api_client

    # Empty accounts
    acc_resp = client.get("/api/accounts")
    assert acc_resp.status_code == 200
    assert acc_resp.json() == {"items": []}

    # Empty metrics summary
    m_empty = client.get("/api/metrics/summary")
    assert m_empty.status_code == 200
    assert m_empty.json() == {
        "total_clips": 0,
        "total_posted": 0,
        "total_views": 0,
        "total_likes": 0,
        "total_watch_minutes": 0.0,
        "items": [],
    }

    # Seed streamer + clip drafts + publication + metric snapshot directly
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

    clip_file = Path(settings.drafts_dir) / "caedrel_2026-11-22_120000_chunk01_draft_1.mp4"
    clip_file.write_bytes(b"fake-mp4-data")
    now = utcnow_iso()

    conn = sqlite3.connect(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO clip_drafts (
                streamer_id, draft_number, title, description, tags_json,
                file_path, duration_sec, score, status, created_at, updated_at
            )
            VALUES (?, 1, 'Insane Play', 'Clip desc', ?, ?, 42.5, 9.2, 'posted', ?, ?)
            """,
            (streamer_id, json.dumps(["gaming", "shorts"]), str(clip_file), now, now),
        )
        clip_id = cur.lastrowid

        pub_cur = conn.execute(
            """
            INSERT INTO publications (
                clip_draft_id, youtube_video_id, youtube_url,
                privacy_status, status, published_at, created_at, updated_at
            )
            VALUES (?, 'vid123', 'https://youtube.com/shorts/vid123', 'public', 'published', ?, ?, ?)
            """,
            (clip_id, now, now, now),
        )
        pub_id = pub_cur.lastrowid

        conn.execute(
            """
            INSERT INTO metric_snapshots (
                publication_id, views, likes, comments, estimated_minutes_watched, captured_at
            )
            VALUES (?, 1500, 120, 14, 45.5, ?)
            """,
            (pub_id, now),
        )
        conn.commit()
    finally:
        conn.close()

    # Filter clips by status and streamer_id
    clips_resp = client.get(f"/api/clips?status=posted&streamer_id={streamer_id}")
    assert clips_resp.status_code == 200
    items = clips_resp.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "Insane Play"
    assert items[0]["tags"] == ["gaming", "shorts"]

    # Non-matching filter returns empty
    empty_clips = client.get("/api/clips?status=draft")
    assert empty_clips.status_code == 200
    assert empty_clips.json() == {"items": []}

    # Metrics summary reflects seeded rows
    metrics_resp = client.get("/api/metrics/summary")
    assert metrics_resp.status_code == 200
    summary = metrics_resp.json()
    assert summary["total_clips"] == 1
    assert summary["total_posted"] == 1
    assert summary["total_views"] == 1500
    assert summary["total_likes"] == 120
    assert summary["total_watch_minutes"] == 45.5
    assert len(summary["items"]) == 1

    # Delete clip removes local file and DB row
    del_resp = client.delete(f"/api/clips/{clip_id}?delete_drive=false")
    assert del_resp.status_code == 200
    assert del_resp.json() == {"ok": True, "deleted_local": True, "deleted_drive": False}
    assert not clip_file.exists()
