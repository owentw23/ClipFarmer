import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from fastapi.testclient import TestClient
import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, utcnow_iso
from app.main import create_app
from app.security import encrypt_token, generate_fernet_key
from app.services.metrics_collector import MetricsCollectorService
from app.services.processing_queue import ProcessingQueueWorker
from app.services.retention_gc import RetentionGC
from app.services.telegram_bot import TelegramBotService
from app.services.youtube_publisher import YouTubePublisherService
from app.storage import ensure_storage_dirs, raw_chunk_filename


def _make_synthetic_mp4(dest: Path, *, width: int = 360, height: int = 640) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s={width}x{height}:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=stereo",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"ffmpeg failed: {res.stderr}"
    return dest


def _seed_account(
    db_path: str,
    settings: Settings,
    *,
    channel_id: str,
    channel_title: str,
    oauth_provider: str = "google",
) -> int:
    now = utcnow_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    enc_access = encrypt_token("e2e-access-token", key=settings.token_encryption_key)
    enc_refresh = encrypt_token("e2e-refresh-token", key=settings.token_encryption_key)
    scopes = (
        "https://www.googleapis.com/auth/drive.file"
        if oauth_provider == "google_drive"
        else (
            "https://www.googleapis.com/auth/youtube.upload "
            "https://www.googleapis.com/auth/youtube.readonly "
            "https://www.googleapis.com/auth/yt-analytics.readonly"
        )
    )
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO youtube_accounts (
                channel_id, channel_title, email, oauth_provider,
                encrypted_refresh_token, encrypted_access_token,
                token_expires_at, scopes, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                channel_id,
                channel_title,
                f"{channel_id.lower().replace(':', '_')}@example.com",
                oauth_provider,
                enc_refresh,
                enc_access,
                expires_at,
                scopes,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_full_pipeline_end_to_end_phase1_through_phase5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Full pipeline E2E test spanning Phases 1–5:
    1. Storage + SQLite init, streamer CRUD + YouTube target routing via FastAPI
    2. Raw recording chunk queued + RetentionGC respects active processing lock
    3. ProcessingQueueWorker (NeonClipping + Copywriter + Google Drive + ffmpeg thumbnail + Telegram notify)
    4. Telegram inline Approve -> YouTubePublisherService resumable upload + Web Reject (keep_local)
    5. MetricsCollectorService (Data API v3 + Analytics API v2 + SHORTS) -> /api/metrics/dashboard + /api/clips/{id}/video + GC cleanup
    """
    with tempfile.TemporaryDirectory(prefix="clip_full_e2e_") as tmp:
        tmp_path = Path(tmp)
        store_root = tmp_path / "Clips_Store"
        raw_dir = store_root / "raw recordings"
        drafts_dir = store_root / "drafts"
        neon_dir = store_root / "neon_tmp"
        db_file = store_root / "db" / "pipeline.sqlite3"

        settings = Settings(
            clips_store_root=str(store_root),
            raw_recordings_dir=str(raw_dir),
            drafts_dir=str(drafts_dir),
            neonclipping_data_dir=str(neon_dir),
            db_path=str(db_file),
            token_encryption_key=generate_fernet_key(),
            llm_api_key="test-key",
            telegram_bot_token="123456:E2E_TOKEN",
            telegram_allowed_chat_ids="777001",
            enable_background_workers=False,
        )
        ensure_storage_dirs(settings)
        asyncio.run(init_db(settings.db_path))

        yt_acc_id = _seed_account(
            settings.db_path,
            settings,
            channel_id="UC_E2E_MAIN",
            channel_title="Main Shorts",
            oauth_provider="google",
        )
        _drive_acc_id = _seed_account(
            settings.db_path,
            settings,
            channel_id="drive-only:e2e",
            channel_title="5TB Drive",
            oauth_provider="google_drive",
        )

        monkeypatch.setattr("app.main.get_settings", lambda: settings)
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings

        tg_sent: list[dict] = []
        tg_answers: list[dict] = []
        yt_uploads: list[str] = []

        def tg_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            url = str(request.url)
            if url.endswith("/sendMessage"):
                tg_sent.append(body)
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "message_id": 500 + len(tg_sent),
                            "chat": {"id": body["chat_id"]},
                        },
                    },
                )
            if url.endswith("/answerCallbackQuery"):
                tg_answers.append(body)
                return httpx.Response(200, json={"ok": True, "result": True})
            if url.endswith("/editMessageReplyMarkup"):
                return httpx.Response(200, json={"ok": True, "result": True})
            return httpx.Response(200, json={"ok": True, "result": {}})

        def yt_upload_handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and "upload/youtube/v3/videos" in str(request.url):
                yt_uploads.append("init")
                return httpx.Response(
                    200,
                    headers={"Location": "https://upload.youtube.example/session/e2e_1"},
                )
            if request.method == "PUT" and "upload.youtube.example/session/e2e_1" in str(request.url):
                yt_uploads.append("bytes")
                return httpx.Response(200, json={"id": "e2e_short_1"})
            raise AssertionError(f"Unexpected YouTube upload call: {request.method} {request.url}")

        def metrics_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "youtube/v3/videos" in url:
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "e2e_short_1",
                                "statistics": {
                                    "viewCount": "18400",
                                    "likeCount": "920",
                                    "commentCount": "54",
                                },
                            }
                        ]
                    },
                )
            if "youtubeanalytics.googleapis.com/v2/reports" in url:
                filters = request.url.params.get("filters", "")
                if "insightTrafficSourceType==SHORTS" in filters:
                    return httpx.Response(
                        200,
                        json={
                            "columnHeaders": [{"name": "video"}, {"name": "views"}],
                            "rows": [["e2e_short_1", 16200]],
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "columnHeaders": [
                            {"name": "video"},
                            {"name": "views"},
                            {"name": "engagedViews"},
                            {"name": "estimatedMinutesWatched"},
                            {"name": "averageViewDuration"},
                            {"name": "averageViewPercentage"},
                            {"name": "likes"},
                            {"name": "comments"},
                            {"name": "shares"},
                            {"name": "subscribersGained"},
                            {"name": "subscribersLost"},
                        ],
                        "rows": [
                            [
                                "e2e_short_1",
                                18000,
                                14500,
                                630.0,
                                31.0,
                                91.4,
                                900,
                                50,
                                88,
                                34,
                                3,
                            ]
                        ],
                    },
                )
            raise AssertionError(f"Unexpected metrics URL: {url}")

        class StubNeon:
            async def arefine_and_export_with_turbo(self, chunk_path, output_dir=None, **kwargs):
                out = Path(output_dir)
                out.mkdir(parents=True, exist_ok=True)
                mp4_1 = _make_synthetic_mp4(out / "clip_1.mp4")
                mp4_2 = _make_synthetic_mp4(out / "clip_2.mp4")
                return {
                    "ok": True,
                    "clips": [
                        {
                            "index": 1,
                            "start": 10.0,
                            "end": 44.0,
                            "duration": 34.0,
                            "score": 96.4,
                            "title": "1v5 Ace Clutch",
                            "hook": "Unbelievable final second defuse",
                            "transcript": "No way he sticks that!",
                            "output_mp4": str(mp4_1),
                        },
                        {
                            "index": 2,
                            "start": 120.0,
                            "end": 168.0,
                            "duration": 48.0,
                            "score": 87.2,
                            "title": "Funny Chat Moment",
                            "hook": "Chat loses it over whiff",
                            "transcript": "Wait what just happened?!",
                            "output_mp4": str(mp4_2),
                        },
                    ],
                }

        class StubCopywriter:
            async def generate_youtube_copy(self, **kwargs):
                base = kwargs.get("raw_title") or kwargs.get("title") or "Highlight"
                return {
                    "title": f"Tarik: {base}",
                    "description": f"Tarik pulls off {base} live on stream!",
                    "tags": ["tarik", "valorant", "shorts"],
                    "source": "llm",
                }

        class StubDrive:
            async def upload_clip(self, db, file_path, **kwargs):
                name = Path(file_path).stem
                return {
                    "drive_file_id": f"drv_{name}",
                    "drive_url": f"https://drive.google.com/file/d/drv_{name}/view",
                }

        with TestClient(app) as client:
            # --- Phase 1: Health, Streamer creation & target routing ---
            health = client.get("/api/health")
            assert health.status_code == 200
            h_body = health.json()
            assert h_body["status"] in {"ok", "degraded"}
            assert h_body["checks"]["database"]["ok"] is True
            assert h_body["checks"]["storage"]["ok"] is True
            assert h_body["checks"]["ffmpeg"]["ok"] is True

            s_resp = client.post(
                "/api/streamers",
                json={
                    "name": "Tarik",
                    "platform": "twitch",
                    "channel_url": "https://twitch.tv/tarik",
                    "auto_watch": True,
                },
            )
            assert s_resp.status_code == 201
            streamer = s_resp.json()
            streamer_id = int(streamer["id"])
            slug = str(streamer["slug"])

            t_resp = client.put(
                f"/api/streamers/{streamer_id}/targets",
                json={
                    "targets": [
                        {
                            "youtube_account_id": yt_acc_id,
                            "default_privacy": "public",
                            "enabled": True,
                        }
                    ]
                },
            )
            assert t_resp.status_code == 200
            assert len(t_resp.json()["items"]) == 1

            # --- Phase 2: Synthetic raw chunk + RetentionGC lock verification ---
            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()
            expired_iso = (now_dt - timedelta(hours=3)).isoformat()
            raw_mp4 = _make_synthetic_mp4(raw_dir / raw_chunk_filename(slug, now_dt, 1), width=320, height=180)

            conn = sqlite3.connect(settings.db_path)
            try:
                sess_id = int(
                    conn.execute(
                        """
                        INSERT INTO stream_sessions (
                            streamer_id, external_stream_id, title, started_at, status, created_at
                        )
                        VALUES (?, 'e2e-live-1', 'Ranked Grind', ?, 'recording', ?)
                        """,
                        (streamer_id, now_iso, now_iso),
                    ).lastrowid
                )
                chunk_id = int(
                    conn.execute(
                        """
                        INSERT INTO recording_chunks (
                            session_id, streamer_id, chunk_index, file_path,
                            duration_sec, merged_with_prev, status, started_at,
                            delete_after, created_at
                        )
                        VALUES (?, ?, 1, ?, 3600.0, 0, 'queued', ?, ?, ?)
                        """,
                        (sess_id, streamer_id, str(raw_mp4), now_iso, expired_iso, now_iso),
                    ).lastrowid
                )
                conn.commit()
            finally:
                conn.close()

            gc_before = asyncio.run(RetentionGC(settings).run_once())
            assert gc_before["deleted_chunk_ids"] == []
            assert raw_mp4.is_file()

            # --- Phase 3 & 4: Queue processing -> Telegram notification -> Approve -> YouTube publish ---
            async def _run_phase3_and_phase4() -> tuple[int, int]:
                async with (
                    httpx.AsyncClient(transport=httpx.MockTransport(tg_handler)) as tg_client,
                    httpx.AsyncClient(transport=httpx.MockTransport(yt_upload_handler)) as yt_client,
                ):
                    publisher = YouTubePublisherService(
                        settings, http_client=yt_client, retry_base_delay=0.0
                    )
                    bot = TelegramBotService(
                        settings, publisher=publisher, http_client=tg_client
                    )
                    worker = ProcessingQueueWorker(
                        settings,
                        neon_client=StubNeon(),
                        copywriter=StubCopywriter(),
                        google_drive=StubDrive(),
                        on_chunk_drafts_ready=bot.notify_chunk_ready,
                    )
                    proc_res = await worker.process_chunk(chunk_id)
                    assert proc_res["ok"] is True
                    assert len(proc_res["clip_ids"]) == 2
                    clip1_id, clip2_id = int(proc_res["clip_ids"][0]), int(proc_res["clip_ids"][1])

                    # Telegram sent batched chunk notification for allowed chat with both drafts
                    assert len(tg_sent) == 1
                    assert "#1" in tg_sent[0]["text"]
                    assert "#2" in tg_sent[0]["text"]
                    assert "Main Shorts" in tg_sent[0]["text"]
                    assert "Open MP4" in tg_sent[0]["text"]

                    # Simulate Telegram user tapping Approve on clip 1
                    await bot.handle_update(
                        {
                            "update_id": 1001,
                            "callback_query": {
                                "id": "cb_approve_1",
                                "from": {"id": 777001, "username": "owner"},
                                "message": {
                                    "message_id": 501,
                                    "chat": {"id": 777001},
                                },
                                "data": f"ap:{clip1_id}",
                            },
                        }
                    )
                    await bot.wait_for_publishes()
                    return clip1_id, clip2_id

            clip1_id, clip2_id = asyncio.run(_run_phase3_and_phase4())
            assert yt_uploads == ["init", "bytes"]
            assert len(tg_answers) == 1

            # Reject clip 2 via API (keep_local) and verify video/thumbnail streaming
            rej = client.post(
                f"/api/clips/{clip2_id}/reject",
                json={"action": "keep_local"},
            )
            assert rej.status_code == 200
            assert rej.json()["status"] == "kept_local"

            vid_resp = client.get(f"/api/clips/{clip1_id}/video")
            assert vid_resp.status_code == 200
            assert vid_resp.headers["content-type"].startswith("video/mp4")

            thumb_resp = client.get(f"/api/clips/{clip1_id}/thumbnail")
            assert thumb_resp.status_code == 200
            assert thumb_resp.headers["content-type"].startswith("image/jpeg")

            # --- Phase 5: Metrics Sync + Dashboard + Summary + Post-process GC ---
            metrics_http = httpx.AsyncClient(transport=httpx.MockTransport(metrics_handler))
            app.state.metrics_collector = MetricsCollectorService(
                settings, http_client=metrics_http, retry_base_delay=0.0
            )
            try:
                sync_resp = client.post("/api/metrics/sync")
                assert sync_resp.status_code == 200
                sync_data = sync_resp.json()
                assert sync_data["synced_publications"] == 1
                assert sync_data["synced_accounts"] == 1
                assert sync_data["analytics_synced"] == 1
                assert sync_data["errors"] == []
            finally:
                asyncio.run(metrics_http.aclose())

            dash_resp = client.get("/api/metrics/dashboard?days=7")
            assert dash_resp.status_code == 200
            dash = dash_resp.json()

            best = dash["best_clip_this_week"]
            assert best is not None
            assert best["clip_draft_id"] == clip1_id
            assert best["youtube_video_id"] == "e2e_short_1"
            assert best["has_local_video"] is True
            assert best["analytics_pending"] is False
            assert best["views"] == 18400
            assert best["avg_view_percentage"] == pytest.approx(91.4)
            assert best["watch_hours"] == pytest.approx(10.5)

            kpis = dash["kpis"]
            assert kpis["total_views"] == 18400
            assert kpis["total_engaged_views"] == 14500
            assert kpis["net_subscribers"] == 31
            assert kpis["posted_count"] == 1
            assert kpis["drafts_generated"] == 2
            assert kpis["approval_rate"] == pytest.approx(50.0)
            assert kpis["shorts_feed_share"] == pytest.approx(88.0)

            assert [s["count"] for s in dash["funnel"]["stages"]] == [2, 2, 1, 1]
            assert dash["funnel"]["outcomes"]["kept_local"] == 1
            assert dash["cohorts"][1]["bucket"] == "30-45s"
            assert dash["cohorts"][1]["clip_count"] == 1
            assert dash["cohorts"][1]["avg_views"] == pytest.approx(18400.0)
            assert len(dash["timeseries"]) == 1
            assert dash["timeseries"][0]["views"] == 18400

            summary = client.get("/api/metrics/summary")
            assert summary.status_code == 200
            s_body = summary.json()
            assert s_body["total_clips"] == 2
            assert s_body["total_posted"] == 1
            assert s_body["total_views"] == 18400

            # Now that chunk is processed and delete_after expired, GC deletes raw chunk only
            gc_after = client.post("/api/system/gc/run")
            assert gc_after.status_code == 200
            assert gc_after.json()["deleted_chunk_ids"] == [chunk_id]
            assert not raw_mp4.exists()
            assert client.get(f"/api/clips/{clip1_id}/video").status_code == 200
