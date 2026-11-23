import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, open_connection, utcnow_iso
from app.routers import accounts, auth, streamers, targets
from app.security import decrypt_token, encrypt_token, generate_fernet_key
from app.services.google_oauth import (
    GOOGLE_OAUTH_SCOPES,
    build_google_auth_url,
    get_valid_access_token,
    refresh_access_token,
)


@pytest.fixture()
def work_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="clip_phase2_oauth_") as tmp:
        yield Path(tmp)


def _make_settings(work_dir: Path, **overrides) -> Settings:
    store_root = work_dir / "Clips_Store"
    base = {
        "clips_store_root": str(store_root),
        "raw_recordings_dir": str(store_root / "raw recordings"),
        "drafts_dir": str(store_root / "drafts"),
        "neonclipping_data_dir": str(store_root / "neon_tmp"),
        "db_path": str(store_root / "db" / "pipeline.sqlite3"),
        "token_encryption_key": generate_fernet_key(),
        "google_client_id": "test-google-client-id",
        "google_client_secret": "test-google-client-secret",
        "google_redirect_uri": "http://127.0.0.1:8000/api/auth/google/callback",
        "frontend_base_url": "http://127.0.0.1:3000",
    }
    base.update(overrides)
    return Settings(**base)


def test_phase2_config_defaults() -> None:
    cfg = Settings()
    assert Settings.model_fields["llm_model"].default == "deepseek-v4-1-flash-economy-261017"
    assert (
        Settings.model_fields["neonclipping_llm_model"].default
        == "deepseek-v4-1-flash-economy-261017"
    )
    assert Settings.model_fields["enable_background_workers"].default is True
    assert cfg.watcher_poll_interval_sec == 60
    assert cfg.chunk_duration_seconds == 7200
    assert cfg.min_standalone_chunk_seconds == 3600
    assert cfg.chunk_retention_seconds == 7200
    assert cfg.vlm_min_score == 5.0
    assert cfg.gc_interval_seconds == 60
    assert cfg.youtube_cookies_file == ""
    assert cfg.frontend_base_url == "http://127.0.0.1:3000"


def test_db_migration_on_preexisting_phase1_db(work_dir: Path) -> None:
    db_file = work_dir / "phase1_legacy.sqlite3"
    now = utcnow_iso()

    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL CHECK(platform IN ('youtube', 'twitch')),
                channel_url TEXT NOT NULL,
                channel_ref TEXT NOT NULL,
                auto_watch INTEGER NOT NULL DEFAULT 1,
                clip_opts_json TEXT NOT NULL,
                is_live INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE youtube_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                channel_title TEXT NOT NULL,
                email TEXT,
                encrypted_refresh_token TEXT,
                encrypted_access_token TEXT,
                token_expires_at TEXT,
                scopes TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE streamer_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
                youtube_account_id INTEGER NOT NULL REFERENCES youtube_accounts(id) ON DELETE CASCADE,
                default_privacy TEXT NOT NULL DEFAULT 'private',
                default_tags_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(streamer_id, youtube_account_id)
            );
            CREATE TABLE stream_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
                external_stream_id TEXT,
                title TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                status TEXT NOT NULL DEFAULT 'recording',
                created_at TEXT NOT NULL
            );
            CREATE TABLE recording_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
                streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                duration_sec REAL,
                merged_with_prev INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'recorded',
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO streamers (name, slug, platform, channel_url, channel_ref, clip_opts_json, created_at, updated_at)
            VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO youtube_accounts (channel_id, channel_title, created_at, updated_at)
            VALUES ('UC_LEGACY', 'Legacy Channel', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO streamer_targets (streamer_id, youtube_account_id, default_privacy, created_at)
            VALUES (1, 1, 'private', ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()

    # Run init_db twice to verify idempotency
    asyncio.run(init_db(db_file))
    asyncio.run(init_db(db_file))

    conn = sqlite3.connect(db_file)
    try:
        chunk_cols = {
            row[1]: row for row in conn.execute("PRAGMA table_info(recording_chunks);").fetchall()
        }
        assert {"started_at", "completed_at", "delete_after", "merged_from_chunk_id"}.issubset(
            chunk_cols.keys()
        )

        target_cols = {
            row[1]: row for row in conn.execute("PRAGMA table_info(streamer_targets);").fetchall()
        }
        assert str(target_cols["default_privacy"][4]).strip("'\"") == "public"

        # Existing row retained its explicit 'private', while new rows omitting default_privacy default to 'public'
        existing_privacy = conn.execute(
            "SELECT default_privacy FROM streamer_targets WHERE id = 1"
        ).fetchone()[0]
        assert existing_privacy == "private"

        conn.execute(
            "INSERT INTO youtube_accounts (channel_id, channel_title, created_at, updated_at) VALUES ('UC_NEW', 'New Channel', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO streamer_targets (streamer_id, youtube_account_id, created_at) VALUES (1, 2, ?)",
            (now,),
        )
        new_privacy = conn.execute(
            "SELECT default_privacy FROM streamer_targets WHERE youtube_account_id = 2"
        ).fetchone()[0]
        assert new_privacy == "public"

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "oauth_states" in tables
        state_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(oauth_states);").fetchall()
        }
        assert state_cols == {"state", "provider", "created_at", "expires_at"}
    finally:
        conn.close()


def _build_test_app(settings: Settings, transport: httpx.MockTransport | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(accounts.router)
    app.include_router(targets.router)
    app.include_router(streamers.router)
    app.dependency_overrides[get_settings] = lambda: settings

    if transport is not None:
        async def _override_http_client():
            async with httpx.AsyncClient(transport=transport) as client:
                yield client

        app.dependency_overrides[auth.get_oauth_http_client] = _override_http_client

    return app


def test_google_auth_start_requires_credentials_and_stores_state(work_dir: Path) -> None:
    unconfigured = _make_settings(work_dir, google_client_id="", google_client_secret="")
    asyncio.run(init_db(unconfigured.db_path))

    app_bad = _build_test_app(unconfigured)
    with TestClient(app_bad) as client:
        resp = client.get("/api/auth/google/start")
        assert resp.status_code == 400

    configured = _make_settings(work_dir)
    app_ok = _build_test_app(configured)
    with TestClient(app_ok) as client:
        resp = client.get("/api/auth/google/start")
        assert resp.status_code == 200
        body = resp.json()
        assert body["configured"] is True
        assert body["state"]

        parsed = urlparse(body["auth_url"])
        qs = parse_qs(parsed.query)
        assert qs["client_id"] == ["test-google-client-id"]
        assert qs["access_type"] == ["offline"]
        assert qs["prompt"] == ["consent"]
        assert qs["include_granted_scopes"] == ["true"]
        assert qs["state"] == [body["state"]]
        scopes = set(qs["scope"][0].split(" "))
        assert set(GOOGLE_OAUTH_SCOPES).issubset(scopes)

    # Verify build_google_auth_url directly
    direct_url = build_google_auth_url(configured, "state-123")
    assert "state=state-123" in direct_url

    # Verify state persisted in DB
    conn = sqlite3.connect(configured.db_path)
    try:
        row = conn.execute(
            "SELECT state, provider FROM oauth_states WHERE state = ?",
            (body["state"],),
        ).fetchone()
        assert row == (body["state"], "google")
    finally:
        conn.close()


def test_google_oauth_callback_csrf_exchange_preserves_refresh_and_drive_fallback(
    work_dir: Path,
) -> None:
    settings = _make_settings(work_dir)
    asyncio.run(init_db(settings.db_path))

    scenario = {"mode": "two_channels_with_refresh", "token_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            scenario["token_calls"] += 1
            body_text = request.content.decode("utf-8")
            form = parse_qs(body_text)
            if form.get("grant_type") == ["authorization_code"]:
                if scenario["mode"] == "reconsent_no_refresh":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "access-tok-v2",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                        },
                    )
                if scenario["mode"] == "no_channels":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "access-tok-drive",
                            "refresh_token": "refresh-tok-drive",
                            "expires_in": 3600,
                            "token_type": "Bearer",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-tok-v1",
                        "refresh_token": "refresh-tok-v1",
                        "expires_in": 3600,
                        "scope": " ".join(GOOGLE_OAUTH_SCOPES),
                        "token_type": "Bearer",
                    },
                )
        if url.startswith("https://www.googleapis.com/oauth2/v2/userinfo"):
            return httpx.Response(
                200,
                json={"id": "google-user-42", "email": "creator@example.com"},
            )
        if url.startswith("https://www.googleapis.com/youtube/v3/channels"):
            if scenario["mode"] == "no_channels":
                return httpx.Response(200, json={"items": []})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "UC_CH_1", "snippet": {"title": "Main Clips"}},
                        {"id": "UC_CH_2", "snippet": {"title": "VOD Highlights"}},
                    ]
                },
            )
        return httpx.Response(404, json={"error": "unexpected url"})

    transport = httpx.MockTransport(handler)
    app = _build_test_app(settings, transport=transport)

    with TestClient(app) as client:
        # 1. Invalid state -> 400
        bad_state = client.get("/api/auth/google/callback?code=abc&state=does-not-exist")
        assert bad_state.status_code == 400

        # 2. Expired state -> 400 and consumed
        start_exp = client.get("/api/auth/google/start").json()
        conn = sqlite3.connect(settings.db_path)
        try:
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            conn.execute(
                "UPDATE oauth_states SET expires_at = ? WHERE state = ?",
                (past, start_exp["state"]),
            )
            conn.commit()
        finally:
            conn.close()
        exp_resp = client.get(
            f"/api/auth/google/callback?code=abc&state={start_exp['state']}&format=json"
        )
        assert exp_resp.status_code == 400

        # 3. Valid callback with format=json -> upserts 2 channels with encrypted tokens
        start1 = client.get("/api/auth/google/start").json()
        cb1 = client.get(
            f"/api/auth/google/callback?code=auth-code-1&state={start1['state']}&format=json"
        )
        assert cb1.status_code == 200
        cb1_json = cb1.json()
        assert cb1_json["ok"] is True
        assert len(cb1_json["accounts"]) == 2
        for acc in cb1_json["accounts"]:
            assert acc["provider"] == "youtube"
            assert acc["has_refresh_token"] is True
            assert "encrypted_access_token" not in acc
            assert "encrypted_refresh_token" not in acc

        # Reusing consumed state must fail
        reused = client.get(
            f"/api/auth/google/callback?code=auth-code-1&state={start1['state']}&format=json"
        )
        assert reused.status_code == 400

        # Verify encrypted tokens stored in DB
        conn = sqlite3.connect(settings.db_path)
        try:
            rows = conn.execute(
                "SELECT channel_id, encrypted_access_token, encrypted_refresh_token FROM youtube_accounts ORDER BY id"
            ).fetchall()
            assert len(rows) == 2
            for ch_id, enc_acc, enc_ref in rows:
                assert enc_acc != "access-tok-v1"
                assert enc_ref != "refresh-tok-v1"
                assert decrypt_token(enc_acc, key=settings.token_encryption_key) == "access-tok-v1"
                assert decrypt_token(enc_ref, key=settings.token_encryption_key) == "refresh-tok-v1"
        finally:
            conn.close()

        # 4. Re-consent callback omitting refresh_token + 302 redirect preserves existing refresh token
        scenario["mode"] = "reconsent_no_refresh"
        start2 = client.get("/api/auth/google/start").json()
        cb2 = client.get(
            f"/api/auth/google/callback?code=auth-code-2&state={start2['state']}",
            follow_redirects=False,
        )
        assert cb2.status_code == 302
        assert (
            cb2.headers["location"]
            == "http://127.0.0.1:3000/accounts?connected=youtube&count=2"
        )

        conn = sqlite3.connect(settings.db_path)
        try:
            rows = conn.execute(
                "SELECT encrypted_access_token, encrypted_refresh_token FROM youtube_accounts"
            ).fetchall()
            for enc_acc, enc_ref in rows:
                assert decrypt_token(enc_acc, key=settings.token_encryption_key) == "access-tok-v2"
                assert decrypt_token(enc_ref, key=settings.token_encryption_key) == "refresh-tok-v1"
        finally:
            conn.close()

        # 5. Zero YouTube channels -> fallback drive-only record
        scenario["mode"] = "no_channels"
        start3 = client.get("/api/auth/google/start").json()
        cb3 = client.get(
            f"/api/auth/google/callback?code=auth-code-3&state={start3['state']}&format=json"
        )
        assert cb3.status_code == 200
        drive_acc = cb3.json()["accounts"]
        assert len(drive_acc) == 1
        assert drive_acc[0]["channel_id"].startswith("drive-only:")
        assert drive_acc[0]["email"] == "creator@example.com"


def test_token_refresh_and_get_valid_access_token(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    asyncio.run(init_db(settings.db_path))

    refresh_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        form = parse_qs(request.content.decode("utf-8"))
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["stored-refresh-token"]
        refresh_calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)

    async def _run() -> None:
        enc_refresh = encrypt_token("stored-refresh-token", key=settings.token_encryption_key)
        enc_valid_access = encrypt_token("still-valid-token", key=settings.token_encryption_key)
        enc_expiring_access = encrypt_token("soon-expiring-token", key=settings.token_encryption_key)
        now = datetime.now(timezone.utc)

        async with httpx.AsyncClient(transport=transport) as client:
            # Direct refresh_access_token test
            refreshed = await refresh_access_token(
                settings,
                enc_refresh,
                settings_key=settings.token_encryption_key,
                http_client=client,
            )
            assert refreshed["access_token"] == "fresh-access-token"
            assert refresh_calls["count"] == 1

            conn = await open_connection(settings.db_path)
            try:
                # Account 1: valid for 1 hour -> should NOT trigger refresh
                cur1 = await conn.execute(
                    """
                    INSERT INTO youtube_accounts (
                        channel_id, channel_title, encrypted_refresh_token,
                        encrypted_access_token, token_expires_at, created_at, updated_at
                    )
                    VALUES ('UC_VALID', 'Valid', ?, ?, ?, ?, ?)
                    """,
                    (
                        enc_refresh,
                        enc_valid_access,
                        (now + timedelta(minutes=30)).isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                acc_valid_id = cur1.lastrowid

                # Account 2: expires in 60s (within 120s buffer) -> MUST refresh & update DB
                cur2 = await conn.execute(
                    """
                    INSERT INTO youtube_accounts (
                        channel_id, channel_title, encrypted_refresh_token,
                        encrypted_access_token, token_expires_at, created_at, updated_at
                    )
                    VALUES ('UC_EXPIRING', 'Expiring', ?, ?, ?, ?, ?)
                    """,
                    (
                        enc_refresh,
                        enc_expiring_access,
                        (now + timedelta(seconds=60)).isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                acc_expiring_id = cur2.lastrowid
                await conn.commit()

                tok1 = await get_valid_access_token(conn, settings, acc_valid_id, http_client=client)
                assert tok1 == "still-valid-token"
                assert refresh_calls["count"] == 1

                tok2 = await get_valid_access_token(conn, settings, acc_expiring_id, http_client=client)
                assert tok2 == "fresh-access-token"
                assert refresh_calls["count"] == 2

                async with conn.execute(
                    "SELECT encrypted_access_token, token_expires_at FROM youtube_accounts WHERE id = ?",
                    (acc_expiring_id,),
                ) as cur:
                    updated_row = await cur.fetchone()
                assert (
                    decrypt_token(
                        updated_row["encrypted_access_token"],
                        key=settings.token_encryption_key,
                    )
                    == "fresh-access-token"
                )
            finally:
                await conn.close()

    asyncio.run(_run())


def test_accounts_delete_and_streamer_targets_routing(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    asyncio.run(init_db(settings.db_path))

    now = utcnow_iso()
    enc_ref = encrypt_token("ref-1", key=settings.token_encryption_key)
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            """
            INSERT INTO youtube_accounts (
                channel_id, channel_title, email, encrypted_refresh_token,
                encrypted_access_token, token_expires_at, scopes, status, created_at, updated_at
            )
            VALUES
                ('UC_A', 'Channel A', 'a@example.com', ?, 'enc', ?, 'scope1', 'active', ?, ?),
                ('UC_B', 'Channel B', 'b@example.com', NULL, 'enc', ?, 'scope2', 'active', ?, ?)
            """,
            (enc_ref, now, now, now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    app = _build_test_app(settings)
    with TestClient(app) as client:
        # Accounts listing
        acc_resp = client.get("/api/accounts")
        assert acc_resp.status_code == 200
        items = acc_resp.json()["items"]
        assert len(items) == 2
        assert items[0]["provider"] == "youtube"
        assert items[0]["has_refresh_token"] is True
        assert items[1]["has_refresh_token"] is False
        for item in items:
            assert "encrypted_access_token" not in item
            assert "encrypted_refresh_token" not in item

        acc1_id = items[0]["id"]
        acc2_id = items[1]["id"]

        # Create a streamer
        s_resp = client.post(
            "/api/streamers",
            json={
                "name": "TenZ",
                "platform": "twitch",
                "channel_url": "https://www.twitch.tv/tenz",
            },
        )
        assert s_resp.status_code == 201
        streamer_id = s_resp.json()["id"]

        # Missing streamer -> 404
        assert client.get("/api/streamers/9999/targets").status_code == 404
        assert (
            client.put("/api/streamers/9999/targets", json={"targets": []}).status_code
            == 404
        )

        # Invalid account_id -> 400
        bad_acc = client.put(
            f"/api/streamers/{streamer_id}/targets",
            json={"targets": [{"youtube_account_id": 9999}]},
        )
        assert bad_acc.status_code == 400

        # Duplicate account_id -> 400
        dup_acc = client.put(
            f"/api/streamers/{streamer_id}/targets",
            json={
                "targets": [
                    {"youtube_account_id": acc1_id},
                    {"youtube_account_id": acc1_id},
                ]
            },
        )
        assert dup_acc.status_code == 400

        # Valid PUT replaces targets (checking default_privacy='public' and enabled=True defaults)
        put1 = client.put(
            f"/api/streamers/{streamer_id}/targets",
            json={
                "targets": [
                    {"youtube_account_id": acc1_id},
                    {
                        "youtube_account_id": acc2_id,
                        "default_privacy": "unlisted",
                        "enabled": False,
                    },
                ]
            },
        )
        assert put1.status_code == 200
        t_items = put1.json()["items"]
        assert put1.json()["streamer_id"] == streamer_id
        assert len(t_items) == 2
        assert t_items[0]["channel_id"] == "UC_A"
        assert t_items[0]["channel_title"] == "Channel A"
        assert t_items[0]["default_privacy"] == "public"
        assert t_items[0]["enabled"] is True
        assert t_items[1]["channel_id"] == "UC_B"
        assert t_items[1]["default_privacy"] == "unlisted"
        assert t_items[1]["enabled"] is False

        # GET returns same targets
        get1 = client.get(f"/api/streamers/{streamer_id}/targets")
        assert get1.status_code == 200
        assert get1.json() == put1.json()

        # Atomic replacement with single target removes acc2
        put2 = client.put(
            f"/api/streamers/{streamer_id}/targets",
            json={"targets": [{"youtube_account_id": acc2_id, "default_privacy": "private"}]},
        )
        assert put2.status_code == 200
        assert len(put2.json()["items"]) == 1
        assert put2.json()["items"][0]["youtube_account_id"] == acc2_id

        # Delete account 2 -> cascades to streamer_targets
        del_acc = client.delete(f"/api/accounts/{acc2_id}")
        assert del_acc.status_code == 200
        assert del_acc.json() == {"ok": True, "id": acc2_id}

        # Deleting missing account -> 404
        assert client.delete(f"/api/accounts/{acc2_id}").status_code == 404

        # Target was cascaded cleanly
        get_after_del = client.get(f"/api/streamers/{streamer_id}/targets")
        assert get_after_del.status_code == 200
        assert get_after_del.json()["items"] == []
