import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
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


@pytest.fixture()
def phase5_env():
    with tempfile.TemporaryDirectory(prefix="clip_phase5_") as tmp:
        tmp_path = Path(tmp)
        store_root = tmp_path / "Clips_Store"
        raw_dir = store_root / "raw recordings"
        drafts_dir = store_root / "drafts"
        neon_dir = store_root / "neon_tmp"
        db_file = store_root / "db" / "pipeline.sqlite3"
        for d in (raw_dir, drafts_dir, neon_dir, db_file.parent):
            d.mkdir(parents=True, exist_ok=True)

        settings = Settings(
            clips_store_root=str(store_root),
            raw_recordings_dir=str(raw_dir),
            drafts_dir=str(drafts_dir),
            neonclipping_data_dir=str(neon_dir),
            db_path=str(db_file),
            token_encryption_key=generate_fernet_key(),
            enable_background_workers=False,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, tmp_path


def _seed_account(
    db_path: str,
    settings: Settings,
    *,
    channel_id: str,
    channel_title: str,
    oauth_provider: str = "google",
    status: str = "active",
) -> int:
    now = utcnow_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    enc_access = encrypt_token("yt-access-token", key=settings.token_encryption_key)
    enc_refresh = encrypt_token("yt-refresh-token", key=settings.token_encryption_key)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO youtube_accounts (
                channel_id, channel_title, email, oauth_provider,
                encrypted_refresh_token, encrypted_access_token,
                token_expires_at, scopes, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                channel_title,
                f"{channel_id.lower()}@example.com",
                oauth_provider,
                enc_refresh,
                enc_access,
                expires_at,
                "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/yt-analytics.readonly",
                status,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_streamer(db_path: str, *, name: str, slug: str) -> int:
    now = utcnow_iso()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES (?, ?, 'twitch', ?, ?, 1, '{}', 0, ?, ?)
            """,
            (name, slug, f"https://twitch.tv/{slug}", slug, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_clip_and_pub(
    db_path: str,
    drafts_dir: Path,
    *,
    streamer_id: int,
    account_id: int | None,
    title: str,
    duration_sec: float,
    status: str = "posted",
    youtube_video_id: str | None = None,
    published_at: str | None = None,
    created_at: str | None = None,
    with_file: bool = True,
) -> tuple[int, int | None]:
    ts = created_at or utcnow_iso()
    file_path = ""
    if with_file:
        mp4 = drafts_dir / f"{title.lower().replace(' ', '_')}_{streamer_id}.mp4"
        mp4.write_bytes(b"ftypmp42")
        file_path = str(mp4)

    conn = sqlite3.connect(db_path)
    try:
        s_cur = conn.execute(
            """
            INSERT INTO stream_sessions (
                streamer_id, external_stream_id, title, started_at, status, created_at
            )
            VALUES (?, 'stream-1', 'Test Stream', ?, 'ended', ?)
            """,
            (streamer_id, ts, ts),
        )
        session_id = int(s_cur.lastrowid)
        ch_cur = conn.execute(
            """
            INSERT INTO recording_chunks (
                session_id, streamer_id, chunk_index, file_path, duration_sec,
                merged_with_prev, status, started_at, created_at
            )
            VALUES (?, ?, 1, ?, 3600.0, 0, 'processed', ?, ?)
            """,
            (session_id, streamer_id, file_path or "chunk.mp4", ts, ts),
        )
        chunk_id = int(ch_cur.lastrowid)
        c_cur = conn.execute(
            """
            INSERT INTO clip_drafts (
                streamer_id, session_id, chunk_id, draft_number, title, description,
                tags_json, file_path, start_sec, end_sec, duration_sec, score, status,
                tg_message_id, created_at, updated_at
            )
            VALUES (?, ?, ?, 1, ?, 'desc', '["shorts"]', ?, 0.0, ?, ?, 0.9, ?, ?, ?, ?)
            """,
            (
                streamer_id,
                session_id,
                chunk_id,
                title,
                file_path,
                duration_sec,
                duration_sec,
                status,
                901 if status in {"notified", "approved", "posted"} else None,
                ts,
                ts,
            ),
        )
        clip_id = int(c_cur.lastrowid)
        pub_id: int | None = None
        if account_id is not None and youtube_video_id is not None:
            pub_ts = published_at or ts
            p_cur = conn.execute(
                """
                INSERT INTO publications (
                    clip_draft_id, youtube_account_id, youtube_video_id, youtube_url,
                    privacy_status, status, published_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'public', 'published', ?, ?, ?)
                """,
                (
                    clip_id,
                    account_id,
                    youtube_video_id,
                    f"https://www.youtube.com/shorts/{youtube_video_id}",
                    pub_ts,
                    pub_ts,
                    pub_ts,
                ),
            )
            pub_id = int(p_cur.lastrowid)
        conn.commit()
        return clip_id, pub_id
    finally:
        conn.close()


def test_schema_migration_adds_analytics_columns(phase5_env):
    settings, _ = phase5_env
    legacy_db = Path(settings.clips_store_root) / "legacy.sqlite3"
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        """
        CREATE TABLE metric_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL,
            views INTEGER NOT NULL DEFAULT 0,
            likes INTEGER NOT NULL DEFAULT 0,
            comments INTEGER NOT NULL DEFAULT 0,
            estimated_minutes_watched REAL NOT NULL DEFAULT 0.0,
            captured_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    asyncio.run(init_db(legacy_db))
    asyncio.run(init_db(legacy_db))

    check = sqlite3.connect(legacy_db)
    cols = {row[1] for row in check.execute("PRAGMA table_info(metric_snapshots);")}
    check.close()
    assert {
        "engaged_views",
        "shares",
        "subscribers_gained",
        "subscribers_lost",
        "avg_view_duration_sec",
        "avg_view_percentage",
        "shorts_feed_views",
        "analytics_synced_at",
    }.issubset(cols)


def test_metrics_collector_syncs_data_and_analytics_and_skips_drive(phase5_env):
    settings, _ = phase5_env
    yt_acc = _seed_account(
        settings.db_path, settings, channel_id="UC_YT_1", channel_title="Main Shorts"
    )
    _drive_acc = _seed_account(
        settings.db_path,
        settings,
        channel_id="drive-only:1",
        channel_title="5TB Drive",
        oauth_provider="google_drive",
    )
    streamer_id = _seed_streamer(settings.db_path, name="Alpha", slug="alpha")
    _, pub_id = _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=streamer_id,
        account_id=yt_acc,
        title="Insane Clutch",
        duration_sec=38.0,
        youtube_video_id="vid_clutch_1",
    )

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "youtube/v3/videos" in url:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "vid_clutch_1",
                            "statistics": {
                                "viewCount": "12500",
                                "likeCount": "640",
                                "commentCount": "38",
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
                        "rows": [["vid_clutch_1", 10900]],
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
                            "vid_clutch_1",
                            11800,
                            9800,
                            420.5,
                            31.2,
                            88.5,
                            610,
                            35,
                            45,
                            22,
                            3,
                        ]
                    ],
                },
            )
        return httpx.Response(404)

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = MetricsCollectorService(settings, http_client=client, retry_base_delay=0.0)
            return await service.sync_all()

    result = asyncio.run(_run())
    assert result["synced_publications"] == 1
    assert result["synced_accounts"] == 1
    assert result["analytics_synced"] == 1
    assert result["errors"] == []

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM metric_snapshots WHERE publication_id = ? ORDER BY id DESC LIMIT 1",
        (pub_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["views"] == 12500
    assert row["likes"] == 640
    assert row["comments"] == 38
    assert row["engaged_views"] == 9800
    assert row["estimated_minutes_watched"] == pytest.approx(420.5)
    assert row["avg_view_duration_sec"] == pytest.approx(31.2)
    assert row["avg_view_percentage"] == pytest.approx(88.5)
    assert row["shares"] == 45
    assert row["subscribers_gained"] == 22
    assert row["subscribers_lost"] == 3
    assert row["shorts_feed_views"] == 10900
    assert row["analytics_synced_at"] is not None


def test_metrics_collector_gracefully_degrades_when_analytics_403(phase5_env):
    settings, _ = phase5_env
    yt_acc = _seed_account(
        settings.db_path, settings, channel_id="UC_YT_403", channel_title="Fallback Ch"
    )
    streamer_id = _seed_streamer(settings.db_path, name="Beta", slug="beta")
    _, pub_id = _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=streamer_id,
        account_id=yt_acc,
        title="Fallback Clip",
        duration_sec=25.0,
        youtube_video_id="vid_fallback_1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "youtube/v3/videos" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "vid_fallback_1",
                            "statistics": {"viewCount": "500", "likeCount": "25", "commentCount": "2"},
                        }
                    ]
                },
            )
        return httpx.Response(403, json={"error": {"message": "Insufficient analytics scope"}})

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            service = MetricsCollectorService(settings, http_client=client, retry_base_delay=0.0)
            return await service.sync_all()

    result = asyncio.run(_run())
    assert result["synced_publications"] == 1
    assert result["analytics_synced"] == 0
    assert len(result["errors"]) == 1

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM metric_snapshots WHERE publication_id = ?", (pub_id,)
    ).fetchone()
    conn.close()
    assert row["views"] == 500
    assert row["likes"] == 25
    assert row["comments"] == 2
    assert row["analytics_synced_at"] is None


def test_metrics_dashboard_best_clip_filters_and_charts(phase5_env):
    settings, _ = phase5_env
    acc1 = _seed_account(settings.db_path, settings, channel_id="UC_1", channel_title="Channel One")
    acc2 = _seed_account(settings.db_path, settings, channel_id="UC_2", channel_title="Channel Two")
    s1 = _seed_streamer(settings.db_path, name="Alpha", slug="alpha")
    s2 = _seed_streamer(settings.db_path, name="Bravo", slug="bravo")

    now = datetime.now(timezone.utc)
    two_days_ago = (now - timedelta(days=2)).isoformat()
    one_day_ago = (now - timedelta(days=1)).isoformat()
    twenty_days_ago = (now - timedelta(days=20)).isoformat()

    _, pub_recent_top = _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=s1,
        account_id=acc1,
        title="Weekly Best Play",
        duration_sec=35.0,
        youtube_video_id="vid_week_top",
        published_at=two_days_ago,
        created_at=two_days_ago,
    )
    _, pub_recent_2 = _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=s2,
        account_id=acc2,
        title="Solid Double",
        duration_sec=52.0,
        youtube_video_id="vid_week_2",
        published_at=one_day_ago,
        created_at=one_day_ago,
    )
    _, pub_old_viral = _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=s1,
        account_id=acc1,
        title="Old Viral Hit",
        duration_sec=22.0,
        youtube_video_id="vid_old_viral",
        published_at=twenty_days_ago,
        created_at=twenty_days_ago,
    )
    _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=s1,
        account_id=None,
        title="Kept Local Clip",
        duration_sec=40.0,
        status="kept_local",
        created_at=one_day_ago,
    )
    _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=s2,
        account_id=None,
        title="Pending Notified",
        duration_sec=40.0,
        status="notified",
        created_at=one_day_ago,
    )

    conn = sqlite3.connect(settings.db_path)
    # Multi-day snapshots on pub_recent_top so 24h delta + timeseries appear
    conn.execute(
        """
        INSERT INTO metric_snapshots (
            publication_id, views, likes, comments, estimated_minutes_watched,
            engaged_views, shares, subscribers_gained, subscribers_lost,
            avg_view_duration_sec, avg_view_percentage, shorts_feed_views,
            analytics_synced_at, captured_at
        )
        VALUES (?, 1000, 50, 2, 30.0, 800, 4, 2, 0, 28.0, 80.0, 900, ?, ?)
        """,
        (pub_recent_top, one_day_ago, (now - timedelta(hours=30)).isoformat()),
    )
    conn.execute(
        """
        INSERT INTO metric_snapshots (
            publication_id, views, likes, comments, estimated_minutes_watched,
            engaged_views, shares, subscribers_gained, subscribers_lost,
            avg_view_duration_sec, avg_view_percentage, shorts_feed_views,
            analytics_synced_at, captured_at
        )
        VALUES (?, 2400, 120, 10, 90.0, 2000, 14, 8, 1, 31.0, 90.0, 2100, ?, ?)
        """,
        (pub_recent_top, now.isoformat(), now.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO metric_snapshots (
            publication_id, views, likes, comments, estimated_minutes_watched,
            engaged_views, shares, subscribers_gained, subscribers_lost,
            avg_view_duration_sec, avg_view_percentage, shorts_feed_views,
            analytics_synced_at, captured_at
        )
        VALUES (?, 600, 30, 5, 30.0, 500, 5, 3, 0, 24.0, 60.0, 500, NULL, ?)
        """,
        (pub_recent_2, now.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO metric_snapshots (
            publication_id, views, likes, comments, estimated_minutes_watched,
            engaged_views, shares, subscribers_gained, subscribers_lost,
            avg_view_duration_sec, avg_view_percentage, shorts_feed_views,
            analytics_synced_at, captured_at
        )
        VALUES (?, 99999, 5000, 200, 1200.0, 80000, 500, 100, 5, 22.0, 95.0, 95000, ?, ?)
        """,
        (pub_old_viral, twenty_days_ago, twenty_days_ago),
    )
    conn.commit()
    conn.close()

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        # Legacy endpoint stays untouched
        legacy = client.get("/api/metrics/summary")
        assert legacy.status_code == 200
        assert set(legacy.json().keys()) == {
            "total_clips",
            "total_posted",
            "total_views",
            "total_likes",
            "total_watch_minutes",
            "items",
        }

        dash_7d = client.get("/api/metrics/dashboard?days=7")
        assert dash_7d.status_code == 200
        d7 = dash_7d.json()
        assert d7["filters"]["window"] == "7d"
        assert d7["best_clip_this_week"]["youtube_video_id"] == "vid_week_top"
        assert d7["best_clip_this_week"]["views"] == 2400
        assert d7["best_clip_this_week"]["views_delta_24h"] == 1400
        assert d7["best_clip_this_week"]["has_local_video"] is True
        assert d7["best_clip_this_week"]["analytics_pending"] is False

        assert d7["kpis"]["total_views"] == 3000
        assert d7["kpis"]["total_engaged_views"] == 2500
        assert d7["kpis"]["watch_hours"] == 2.0
        assert d7["kpis"]["net_subscribers"] == 10
        assert d7["kpis"]["posted_count"] == 2
        assert d7["kpis"]["drafts_generated"] == 4
        assert [c["bucket"] for c in d7["cohorts"]] == ["<30s", "30-45s", "45-60s", "60s+"]
        assert d7["cohorts"][1]["clip_count"] == 1
        assert d7["cohorts"][2]["clip_count"] == 1
        assert len(d7["timeseries"]) >= 2
        assert [s["stage"] for s in d7["funnel"]["stages"]] == [
            "Generated",
            "Notified",
            "Approved",
            "Posted",
        ]

        # 30d window includes old viral clip in KPIs while best_clip_this_week remains this week
        dash_30d = client.get("/api/metrics/dashboard?days=30")
        assert dash_30d.status_code == 200
        d30 = dash_30d.json()
        assert d30["kpis"]["total_views"] == 102999
        assert d30["best_clip_this_week"]["youtube_video_id"] == "vid_week_top"

        # Streamer filter isolates Bravo
        filtered = client.get(f"/api/metrics/dashboard?days=7&streamer_id={s2}")
        assert filtered.status_code == 200
        f_data = filtered.json()
        assert f_data["kpis"]["total_views"] == 600
        assert f_data["best_clip_this_week"]["youtube_video_id"] == "vid_week_2"
        assert f_data["best_clip_this_week"]["analytics_pending"] is True


def test_metrics_sync_endpoint(phase5_env):
    settings, _ = phase5_env
    acc = _seed_account(settings.db_path, settings, channel_id="UC_SYNC", channel_title="Sync Ch")
    sid = _seed_streamer(settings.db_path, name="SyncStreamer", slug="syncstreamer")
    _seed_clip_and_pub(
        settings.db_path,
        Path(settings.drafts_dir),
        streamer_id=sid,
        account_id=acc,
        title="Sync Clip",
        duration_sec=32.0,
        youtube_video_id="vid_sync_1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "youtube/v3/videos" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "vid_sync_1",
                            "statistics": {"viewCount": "777", "likeCount": "33", "commentCount": "4"},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"columnHeaders": [{"name": "video"}], "rows": []})

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        transport = httpx.MockTransport(handler)
        async_client = httpx.AsyncClient(transport=transport)
        app.state.metrics_collector = MetricsCollectorService(
            settings, http_client=async_client, retry_base_delay=0.0
        )
        try:
            resp = client.post("/api/metrics/sync")
        finally:
            asyncio.run(async_client.aclose())
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["synced_publications"] == 1
        assert payload["errors"] == []
