import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

from fastapi.testclient import TestClient
import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, open_connection, utcnow_iso
from app.main import create_app
from app.security import encrypt_token, generate_fernet_key
from app.services.processing_queue import ProcessingQueueWorker
from app.services.telegram_bot import TelegramBotError, TelegramBotService, parse_allowed_chat_ids
from app.services.youtube_publisher import YouTubePublisherError, YouTubePublisherService
from app.storage import draft_clip_filename, raw_chunk_filename


@pytest.fixture()
def phase4_env():
    with tempfile.TemporaryDirectory(prefix="clip_phase4_") as tmp:
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
            llm_base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
            llm_model="deepseek-v4-1-flash-261017",
            llm_api_key="test-llm-key",
            telegram_bot_token="123456:TEST_BOT_TOKEN_SECRET",
            telegram_allowed_chat_ids="1001, -100999",
            enable_background_workers=False,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, tmp_path


def _seed_youtube_account(
    db_path: str,
    settings: Settings,
    *,
    channel_id: str,
    channel_title: str,
    oauth_provider: str = "google",
    scopes: str = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/userinfo.email",
    status: str = "active",
    access_token: str = "yt-access-token",
) -> int:
    now = utcnow_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    enc_access = encrypt_token(access_token, key=settings.token_encryption_key)
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
                scopes,
                status,
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_streamer_and_target(
    db_path: str,
    *,
    slug: str = "tarik",
    name: str = "Tarik",
    account_id: int | None = None,
    default_privacy: str = "public",
    enabled: int = 1,
) -> int:
    now = utcnow_iso()
    conn = sqlite3.connect(db_path)
    try:
        existing = conn.execute("SELECT id FROM streamers WHERE slug = ?", (slug,)).fetchone()
        if existing:
            streamer_id = int(existing[0])
        else:
            cur = conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES (?, ?, 'twitch', ?, ?, 1, '{}', 1, ?, ?)
                """,
                (name, slug, f"https://twitch.tv/{slug}", slug, now, now),
            )
            streamer_id = int(cur.lastrowid)

        if account_id is not None:
            conn.execute(
                """
                INSERT INTO streamer_targets (
                    streamer_id, youtube_account_id, default_privacy,
                    default_tags_json, enabled, created_at
                )
                VALUES (?, ?, ?, '[]', ?, ?)
                """,
                (streamer_id, account_id, default_privacy, enabled, now),
            )
        conn.commit()
        return streamer_id
    finally:
        conn.close()


def _seed_chunk_and_clips(
    settings: Settings,
    *,
    streamer_id: int,
    slug: str = "tarik",
    count: int = 1,
    titles: list[str] | None = None,
    descriptions: list[str] | None = None,
    drive_urls: list[str | None] | None = None,
    status: str = "draft",
) -> tuple[int, list[int]]:
    dt = datetime(2026, 11, 22, 15, 0, 0, tzinfo=timezone.utc)
    dt_iso = dt.isoformat()
    raw_file = Path(settings.raw_recordings_dir) / raw_chunk_filename(slug, dt, 1)
    raw_file.write_bytes(b"fake-raw-chunk")

    conn = sqlite3.connect(settings.db_path)
    try:
        s_cur = conn.execute(
            """
            INSERT INTO stream_sessions (
                streamer_id, external_stream_id, title, started_at, status, created_at
            )
            VALUES (?, 'stream-1', 'Live Stream Title <Special & "Test">', ?, 'recording', ?)
            """,
            (streamer_id, dt_iso, dt_iso),
        )
        session_id = int(s_cur.lastrowid)

        c_cur = conn.execute(
            """
            INSERT INTO recording_chunks (
                session_id, streamer_id, chunk_index, file_path, duration_sec,
                merged_with_prev, status, started_at, created_at
            )
            VALUES (?, ?, 1, ?, 3600.0, 0, 'processed', ?, ?)
            """,
            (session_id, streamer_id, str(raw_file), dt_iso, dt_iso),
        )
        chunk_id = int(c_cur.lastrowid)

        clip_ids: list[int] = []
        for idx in range(1, count + 1):
            mp4_path = Path(settings.drafts_dir) / draft_clip_filename(slug, dt, 1, idx, ext="mp4")
            mp4_path.write_bytes(f"fake-clip-video-bytes-{idx:02d}".encode("utf-8"))
            jpg_path = Path(settings.drafts_dir) / draft_clip_filename(slug, dt, 1, idx, ext="jpg")
            jpg_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpg")

            title = titles[idx - 1] if titles and idx - 1 < len(titles) else f"Clip Title {idx}"
            desc = (
                descriptions[idx - 1]
                if descriptions and idx - 1 < len(descriptions)
                else f"One sentence summary for clip {idx}."
            )
            d_url = (
                drive_urls[idx - 1]
                if drive_urls and idx - 1 < len(drive_urls)
                else f"https://drive.google.com/file/d/drv_{idx}/view"
            )
            d_id = f"drv_{idx}" if d_url else None

            cur = conn.execute(
                """
                INSERT INTO clip_drafts (
                    streamer_id, session_id, chunk_id, draft_number,
                    title, description, tags_json, file_path, thumbnail_path,
                    drive_file_id, drive_url, start_sec, end_sec, duration_sec,
                    score, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 35.0, 92.5, ?, ?, ?)
                """,
                (
                    streamer_id,
                    session_id,
                    chunk_id,
                    idx,
                    title,
                    desc,
                    json.dumps(["shorts", "gaming"]),
                    str(mp4_path),
                    str(jpg_path),
                    d_id,
                    d_url,
                    float((idx - 1) * 60 + 10),
                    float((idx - 1) * 60 + 45),
                    status,
                    dt_iso,
                    dt_iso,
                ),
            )
            clip_ids.append(int(cur.lastrowid))
        conn.commit()
        return chunk_id, clip_ids
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. YouTubePublisherService single-target, multi-target & chunked upload
# ---------------------------------------------------------------------------


def test_youtube_publisher_single_and_multi_target_with_privacy_override(
    phase4_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.youtube_publisher as pub_mod

    monkeypatch.setattr(pub_mod, "RESUMABLE_CHUNK_SIZE", 10)
    settings, _ = phase4_env

    acc1 = _seed_youtube_account(
        settings.db_path,
        settings,
        channel_id="UC_YT_1",
        channel_title="Main Shorts",
        access_token="tok-ch-1",
    )
    acc2 = _seed_youtube_account(
        settings.db_path,
        settings,
        channel_id="UC_YT_2",
        channel_title="Clip Archive",
        access_token="tok-ch-2",
    )
    streamer_id = _seed_streamer_and_target(
        settings.db_path, account_id=acc1, default_privacy="public"
    )
    _seed_streamer_and_target(
        settings.db_path, account_id=acc2, default_privacy="unlisted"
    )

    long_title = "T" * 120
    _, [clip_id] = _seed_chunk_and_clips(
        settings,
        streamer_id=streamer_id,
        count=1,
        titles=[long_title],
        descriptions=["Insane clutch play in overtime!"],
    )

    seen_inits: list[dict] = []
    seen_chunks: list[tuple[str, str | None, bytes]] = []
    upload_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_counter
        parsed = urlparse(str(request.url))
        if request.method == "POST" and parsed.path == "/upload/youtube/v3/videos":
            upload_counter += 1
            body = json.loads(request.content.decode("utf-8"))
            seen_inits.append(
                {
                    "auth": request.headers.get("Authorization"),
                    "x_len": request.headers.get("X-Upload-Content-Length"),
                    "body": body,
                }
            )
            return httpx.Response(
                200,
                headers={"Location": f"https://www.googleapis.com/upload/session/{upload_counter}"},
            )
        if request.method == "PUT" and parsed.path.startswith("/upload/session/"):
            sess_idx = int(parsed.path.rsplit("/", 1)[-1])
            content_range = request.headers.get("Content-Range")
            seen_chunks.append((parsed.path, content_range, request.content))
            # 24-byte payload split into 10 + 10 + 4 bytes
            if content_range and not content_range.startswith("bytes 20-"):
                return httpx.Response(308)
            return httpx.Response(200, json={"id": f"yt_vid_{sess_idx}"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def _run():
        conn = await open_connection(settings.db_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                svc = YouTubePublisherService(settings, http_client=client, retry_base_delay=0.0)
                # Publish to both targets with default per-target privacy
                res_multi = await svc.publish_clip(conn, clip_id)
                assert res_multi["status"] == "posted"
                assert res_multi["succeeded"] == 2
                assert res_multi["failed"] == 0
                assert len(res_multi["publications"]) == 2

                # Publish to single target with private override
                res_single = await svc.publish_clip(
                    conn, clip_id, privacy_override="private", account_ids=[acc1]
                )
                assert res_single["succeeded"] == 1
                assert res_single["publications"][0]["privacy_status"] == "private"
                return await svc.list_publications(conn, clip_id)
        finally:
            await conn.close()

    pubs = asyncio.run(_run())
    assert len(pubs) == 3
    assert seen_inits[0]["auth"] == "Bearer tok-ch-1"
    assert seen_inits[0]["body"]["snippet"]["title"] == "T" * 100
    assert seen_inits[0]["body"]["snippet"]["categoryId"] == "22"
    assert seen_inits[0]["body"]["status"] == {
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
    }
    assert seen_inits[1]["auth"] == "Bearer tok-ch-2"
    assert seen_inits[1]["body"]["status"]["privacyStatus"] == "unlisted"
    assert seen_inits[2]["body"]["status"]["privacyStatus"] == "private"
    # Verify multi-chunk upload sent 3 chunks per publish (9 PUTs total)
    assert len(seen_chunks) == 9
    assert seen_chunks[0][1] == "bytes 0-9/24"
    assert seen_chunks[2][1] == "bytes 20-23/24"

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        clip_row = conn.execute("SELECT status FROM clip_drafts WHERE id = ?", (clip_id,)).fetchone()
        assert clip_row["status"] == "posted"
        pub_urls = {
            r["youtube_url"]
            for r in conn.execute("SELECT youtube_url FROM publications WHERE clip_draft_id = ?", (clip_id,)).fetchall()
        }
        assert "https://www.youtube.com/shorts/yt_vid_1" in pub_urls
        assert "https://www.youtube.com/shorts/yt_vid_2" in pub_urls
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. YouTubePublisherService skips Google Drive accounts & handles no targets
# ---------------------------------------------------------------------------


def test_youtube_publisher_skips_google_drive_accounts_and_raises_when_empty(phase4_env) -> None:
    settings, _ = phase4_env
    drive_only_acc = _seed_youtube_account(
        settings.db_path,
        settings,
        channel_id="drive-only:storage@example.com",
        channel_title="5TB Drive",
        oauth_provider="google_drive",
        scopes="https://www.googleapis.com/auth/drive.file",
    )
    drive_provider_acc = _seed_youtube_account(
        settings.db_path,
        settings,
        channel_id="UC_FAKE_DRIVE",
        channel_title="Drive Provider",
        oauth_provider="google_drive",
        scopes="https://www.googleapis.com/auth/youtube.upload",
    )
    disabled_yt_acc = _seed_youtube_account(
        settings.db_path,
        settings,
        channel_id="UC_DISABLED",
        channel_title="Disabled YT",
    )
    streamer_id = _seed_streamer_and_target(
        settings.db_path, account_id=drive_only_acc, enabled=1
    )
    _seed_streamer_and_target(settings.db_path, account_id=drive_provider_acc, enabled=1)
    _seed_streamer_and_target(settings.db_path, account_id=disabled_yt_acc, enabled=0)

    _, [clip_id] = _seed_chunk_and_clips(settings, streamer_id=streamer_id, count=1)

    async def _run():
        conn = await open_connection(settings.db_path)
        try:
            svc = YouTubePublisherService(settings, retry_base_delay=0.0)
            targets = await svc.resolve_targets(conn, streamer_id)
            assert targets == []
            with pytest.raises(YouTubePublisherError, match="No enabled YouTube publishing targets"):
                await svc.publish_clip(conn, clip_id)
        finally:
            await conn.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. YouTubePublisherService retries transient 429/503 and isolates partial failure
# ---------------------------------------------------------------------------


def test_youtube_publisher_retries_transient_and_isolates_partial_failure(phase4_env) -> None:
    settings, _ = phase4_env
    acc_ok = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_OK", channel_title="Good Channel", access_token="tok-ok"
    )
    acc_bad = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_BAD", channel_title="Bad Channel", access_token="tok-bad"
    )
    streamer_id = _seed_streamer_and_target(settings.db_path, account_id=acc_ok)
    _seed_streamer_and_target(settings.db_path, account_id=acc_bad)
    _, [clip_id] = _seed_chunk_and_clips(settings, streamer_id=streamer_id, count=1)

    attempts = {"ok_init": 0, "ok_put": 0, "bad_init": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization")
        if request.method == "POST":
            if auth == "Bearer tok-ok":
                attempts["ok_init"] += 1
                if attempts["ok_init"] == 1:
                    return httpx.Response(503, text="backend unavailable")
                return httpx.Response(
                    200,
                    headers={"Location": "https://upload.youtube.example/ok_session"},
                )
            if auth == "Bearer tok-bad":
                attempts["bad_init"] += 1
                # Non-retryable 403 must NOT retry
                return httpx.Response(403, text="quotaExceeded")
        if request.method == "PUT" and str(request.url) == "https://upload.youtube.example/ok_session":
            attempts["ok_put"] += 1
            if attempts["ok_put"] == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(201, json={"id": "yt_recovered_1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def _run():
        conn = await open_connection(settings.db_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                svc = YouTubePublisherService(
                    settings, http_client=client, max_retries=2, retry_base_delay=0.0
                )
                return await svc.publish_clip(conn, clip_id)
        finally:
            await conn.close()

    res = asyncio.run(_run())
    assert attempts["ok_init"] == 2
    assert attempts["ok_put"] == 2
    assert attempts["bad_init"] == 1
    assert res["status"] == "posted"
    assert res["succeeded"] == 1
    assert res["failed"] == 1
    by_acc = {p["youtube_account_id"]: p for p in res["publications"]}
    assert by_acc[acc_ok]["status"] == "published"
    assert by_acc[acc_ok]["youtube_video_id"] == "yt_recovered_1"
    assert by_acc[acc_bad]["status"] == "failed"
    assert "403" in (by_acc[acc_bad]["error_message"] or "")


# ---------------------------------------------------------------------------
# 4. YouTubePublisherService path containment & HTTPS Location validation
# ---------------------------------------------------------------------------


def test_youtube_publisher_rejects_outside_drafts_dir_and_insecure_location(phase4_env) -> None:
    settings, tmp_path = phase4_env
    acc = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_SEC", channel_title="Sec Channel"
    )
    streamer_id = _seed_streamer_and_target(settings.db_path, account_id=acc)
    _, [clip_id] = _seed_chunk_and_clips(settings, streamer_id=streamer_id, count=1)

    # Create a sensitive file outside drafts_dir and point clip_drafts.file_path to it
    outside_file = tmp_path / "outside_secret.mp4"
    outside_file.write_bytes(b"secret-outside-bytes")
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE clip_drafts SET file_path = ? WHERE id = ?",
            (str(outside_file), clip_id),
        )
        conn.commit()
    finally:
        conn.close()

    async def _run_traversal():
        db = await open_connection(settings.db_path)
        try:
            svc = YouTubePublisherService(settings, retry_base_delay=0.0)
            with pytest.raises(ValueError, match="outside drafts_dir"):
                await svc.publish_clip(db, clip_id)
        finally:
            await db.close()

    asyncio.run(_run_traversal())

    # Restore valid file inside drafts_dir, then return http:// Location header
    _, [valid_clip_id] = _seed_chunk_and_clips(settings, streamer_id=streamer_id, count=1)

    def insecure_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Location": "http://insecure.example/upload"})

    async def _run_insecure_loc():
        db = await open_connection(settings.db_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(insecure_handler)) as client:
                svc = YouTubePublisherService(settings, http_client=client, retry_base_delay=0.0)
                return await svc.publish_clip(db, valid_clip_id)
        finally:
            await db.close()

    res = asyncio.run(_run_insecure_loc())
    assert res["status"] == "failed"
    assert res["succeeded"] == 0
    assert res["failed"] == 1
    assert "HTTPS Location" in (res["publications"][0]["error_message"] or "")


# ---------------------------------------------------------------------------
# 5. TelegramBotService.notify_chunk_ready (escaping, <=4096, <64B callbacks, token scrub)
# ---------------------------------------------------------------------------


def test_telegram_notify_chunk_ready_escaping_length_callbacks_and_token_scrub(phase4_env) -> None:
    settings, _ = phase4_env
    yt_acc = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_MAIN", channel_title="Tarik <Shorts & Clips>"
    )
    drive_acc = _seed_youtube_account(
        settings.db_path,
        settings,
        channel_id="drive-only:5tb@example.com",
        channel_title="5TB Drive Should Not Appear",
        oauth_provider="google_drive",
    )
    streamer_id = _seed_streamer_and_target(settings.db_path, account_id=yt_acc)
    _seed_streamer_and_target(settings.db_path, account_id=drive_acc)

    chunk_id, clip_ids = _seed_chunk_and_clips(
        settings,
        streamer_id=streamer_id,
        count=3,
        titles=[
            '<script>alert(1)</script> & "Wild"',
            "Long " + ("A" * 1600),
            "Clip 3",
        ],
        descriptions=[
            'Check <a href="x">this</a> & more.',
            "Desc " + ("B" * 2000),
            "Third description.",
        ],
        drive_urls=[
            "https://drive.google.com/file/d/1/view",
            "https://drive.google.com/file/d/2/view",
            None,
        ],
    )

    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sent_payloads.append({"url": str(request.url), "body": body})
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 777, "chat": {"id": body["chat_id"]}}},
        )

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            bot = TelegramBotService(settings, http_client=client)
            return await bot.notify_chunk_ready(chunk_id, clip_ids)

    msgs = asyncio.run(_run())
    # Allowed chats: 1001 and -100999 (sorted order: -100999, 1001)
    assert len(msgs) == 2
    assert len(sent_payloads) == 2
    first_body = sent_payloads[0]["body"]
    text = first_body["text"]
    assert first_body["parse_mode"] == "HTML"
    assert first_body["disable_web_page_preview"] is True
    assert len(text) <= 4096

    # HTML escaped dynamic values & Drive-only channel excluded from Targets
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;Wild&quot;" in text
    assert "Tarik &lt;Shorts &amp; Clips&gt;" in text
    assert "5TB Drive Should Not Appear" not in text
    assert "https://drive.google.com/file/d/1/view" in text
    assert "Drive: Not uploaded" in text

    # Inline keyboard has 3 clip rows + 1 Approve All / Reject All row, all callbacks < 64 bytes
    kb = first_body["reply_markup"]["inline_keyboard"]
    assert len(kb) == 4
    assert kb[0][0]["callback_data"] == f"ap:{clip_ids[0]}"
    assert kb[0][1]["callback_data"] == f"rj:{clip_ids[0]}"
    assert kb[3][0]["callback_data"] == f"aa:{chunk_id}"
    assert kb[3][1]["callback_data"] == f"ra:{chunk_id}"
    for row in kb:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) < 64

    # DB updated with tg_chat_id, tg_message_id, status='notified'
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT tg_chat_id, tg_message_id, status FROM clip_drafts WHERE chunk_id = ? ORDER BY id ASC",
            (chunk_id,),
        ).fetchall()
        assert len(rows) == 3
        for r in rows:
            assert r["tg_chat_id"] == "-100999"
            assert r["tg_message_id"] == 777
            assert r["status"] == "notified"
    finally:
        conn.close()

    # Verify bot token is scrubbed from network error messages
    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"Failed calling {request.url}", request=request)

    async def _run_err():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as client:
            bot = TelegramBotService(settings, http_client=client)
            with pytest.raises(TelegramBotError) as exc_info:
                await bot._api_call("getUpdates")
            assert settings.telegram_bot_token not in str(exc_info.value)
            assert "bot<redacted>" in str(exc_info.value)

    asyncio.run(_run_err())


# ---------------------------------------------------------------------------
# 6. TelegramBotService.handle_update (deny-by-default auth + ap/rj/aa/ra)
# ---------------------------------------------------------------------------


def test_telegram_handle_update_authorization_and_callbacks(phase4_env) -> None:
    settings, _ = phase4_env
    acc = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_TG", channel_title="TG Shorts"
    )
    streamer_id = _seed_streamer_and_target(settings.db_path, account_id=acc)
    chunk_id, clip_ids = _seed_chunk_and_clips(
        settings, streamer_id=streamer_id, count=3, status="notified"
    )
    c1, c2, c3 = clip_ids

    # Deny-by-default when allowlist is empty
    empty_settings = settings.model_copy(update={"telegram_allowed_chat_ids": ""})
    empty_bot = TelegramBotService(empty_settings)
    assert empty_bot.is_configured() is False
    assert empty_bot.is_authorized_chat(1001) is False
    assert parse_allowed_chat_ids("1001, bad, -100999") == {"1001", "-100999"}

    tg_calls: list[tuple[str, dict]] = []
    yt_vid_seq = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal yt_vid_seq
        url = str(request.url)
        if url.startswith("https://api.telegram.org/"):
            method = url.rsplit("/", 1)[-1]
            body = json.loads(request.content.decode("utf-8"))
            tg_calls.append((method, body))
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.method == "POST" and "upload/youtube/v3/videos" in url:
            yt_vid_seq += 1
            return httpx.Response(
                200, headers={"Location": f"https://www.googleapis.com/yt_sess/{yt_vid_seq}"}
            )
        if request.method == "PUT" and "yt_sess" in url:
            sid = url.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": f"tg_yt_{sid}"})
        raise AssertionError(f"Unexpected HTTP call: {request.method} {url}")

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = YouTubePublisherService(settings, http_client=client, retry_base_delay=0.0)
            bot = TelegramBotService(settings, publisher=publisher, http_client=client)

            # 1) Unauthorized chat ID rejected
            unauth = await bot.handle_update(
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "cb-unauth",
                        "data": f"ap:{c1}",
                        "message": {"chat": {"id": 999999}, "message_id": 50},
                    },
                }
            )
            assert unauth == {"ok": False, "reason": "unauthorized"}

            # 2) Approve clip #1 with unlisted override
            ap1 = await bot.handle_update(
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb-ap1",
                        "data": f"ap:{c1}:unlisted",
                        "message": {"chat": {"id": 1001}, "message_id": 50},
                    },
                }
            )
            assert ap1["ok"] is True
            assert ap1["queued_count"] == 1
            ap1_jobs = await bot.wait_for_publishes()
            assert [j["posted_count"] for j in ap1_jobs] == [1]

            # 3) Reject clip #2
            rj2 = await bot.handle_update(
                {
                    "update_id": 3,
                    "callback_query": {
                        "id": "cb-rj2",
                        "data": f"rj:{c2}",
                        "message": {"chat": {"id": -100999}, "message_id": 51},
                    },
                }
            )
            assert rj2["ok"] is True
            assert rj2["rejected_count"] == 1

            # 4) Approve All on chunk (skips already-posted c1, publishes c2 & c3)
            aa = await bot.handle_update(
                {
                    "update_id": 4,
                    "callback_query": {
                        "id": "cb-aa",
                        "data": f"aa:{chunk_id}",
                        "message": {"chat": {"id": 1001}, "message_id": 50},
                    },
                }
            )
            assert aa["ok"] is True
            assert aa["queued_count"] == 2
            assert aa["skipped_count"] == 1
            aa_jobs = await bot.wait_for_publishes()
            assert [j["posted_count"] for j in aa_jobs] == [2]

            # 5) Reject All on a separate chunk marks remaining drafts kept_local
            chunk2_id, chunk2_clips = _seed_chunk_and_clips(
                settings, streamer_id=streamer_id, slug="tarik2", count=2, status="notified"
            )
            ra = await bot.handle_update(
                {
                    "update_id": 5,
                    "callback_query": {
                        "id": "cb-ra",
                        "data": f"ra:{chunk2_id}",
                        "message": {"chat": {"id": 1001}, "message_id": 52},
                    },
                }
            )
            assert ra["ok"] is True
            assert ra["rejected_count"] == 2
            return chunk2_clips

    chunk2_clips = asyncio.run(_run())

    # Verify unauthorized alert and editMessageText calls
    assert tg_calls[0] == (
        "answerCallbackQuery",
        {"callback_query_id": "cb-unauth", "text": "Unauthorized chat", "show_alert": True},
    )
    edit_calls = [body for method, body in tg_calls if method == "editMessageText"]
    # ap: immediate + after clip; rj: 1; aa: immediate + per clip (2); ra: 1
    assert len(edit_calls) == 7
    assert "publishing" in edit_calls[0]["text"]
    assert "https://www.youtube.com/shorts/tg_yt_1" in edit_calls[1]["text"]

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        statuses = {
            r["id"]: r["status"]
            for r in conn.execute(
                "SELECT id, status FROM clip_drafts WHERE id IN (?, ?, ?, ?, ?)",
                (c1, c2, c3, chunk2_clips[0], chunk2_clips[1]),
            ).fetchall()
        }
        assert statuses[c1] == "posted"
        assert statuses[c2] == "posted"
        assert statuses[c3] == "posted"
        assert statuses[chunk2_clips[0]] == "kept_local"
        assert statuses[chunk2_clips[1]] == "kept_local"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 7. ProcessingQueueWorker -> TelegramBotService hook + pre-Phase-4 DB migration
# ---------------------------------------------------------------------------


def _make_synthetic_mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=180x320:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1.0",
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
    assert proc.returncode == 0, f"ffmpeg failed: {proc.stderr}"
    return path


def test_db_migration_and_processing_queue_to_telegram_hook(phase4_env) -> None:
    settings, tmp_path = phase4_env

    # Verify idempotent migration on a pre-Phase-4 DB missing tg_chat_id, tg_message_id, last_error
    legacy_db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(legacy_db)
    try:
        conn.executescript(
            """
            CREATE TABLE streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL, channel_url TEXT NOT NULL, channel_ref TEXT NOT NULL,
                auto_watch INTEGER NOT NULL DEFAULT 1, clip_opts_json TEXT NOT NULL,
                is_live INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE youtube_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL UNIQUE,
                channel_title TEXT NOT NULL, email TEXT, encrypted_refresh_token TEXT,
                encrypted_access_token TEXT, token_expires_at TEXT, scopes TEXT,
                status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE streamer_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, streamer_id INTEGER NOT NULL,
                youtube_account_id INTEGER NOT NULL, default_privacy TEXT NOT NULL DEFAULT 'private',
                default_tags_json TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE stream_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, streamer_id INTEGER NOT NULL,
                external_stream_id TEXT, title TEXT, started_at TEXT NOT NULL,
                ended_at TEXT, status TEXT NOT NULL DEFAULT 'recording', created_at TEXT NOT NULL
            );
            CREATE TABLE recording_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
                streamer_id INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
                file_path TEXT NOT NULL, duration_sec REAL,
                merged_with_prev INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'recorded',
                created_at TEXT NOT NULL
            );
            CREATE TABLE clip_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, streamer_id INTEGER NOT NULL,
                session_id INTEGER, chunk_id INTEGER, draft_number INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
                tags_json TEXT NOT NULL DEFAULT '[]', file_path TEXT NOT NULL,
                thumbnail_path TEXT, drive_file_id TEXT, drive_url TEXT,
                start_sec REAL, end_sec REAL, duration_sec REAL, score REAL,
                status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    asyncio.run(init_db(legacy_db))
    conn = sqlite3.connect(legacy_db)
    try:
        clip_cols = {r[1] for r in conn.execute("PRAGMA table_info(clip_drafts);").fetchall()}
        chunk_cols = {r[1] for r in conn.execute("PRAGMA table_info(recording_chunks);").fetchall()}
        assert {"tg_chat_id", "tg_message_id"}.issubset(clip_cols)
        assert "last_error" in chunk_cols
    finally:
        conn.close()

    # End-to-end ProcessingQueueWorker -> TelegramBotService.notify_chunk_ready
    acc = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_HOOK", channel_title="Hook Channel"
    )
    streamer_id = _seed_streamer_and_target(settings.db_path, slug="caedrel", name="Caedrel", account_id=acc)
    dt = datetime(2026, 11, 22, 16, 0, 0, tzinfo=timezone.utc)
    dt_iso = dt.isoformat()
    raw_file = Path(settings.raw_recordings_dir) / raw_chunk_filename("caedrel", dt, 1)
    raw_file.write_bytes(b"raw-video")

    conn = sqlite3.connect(settings.db_path)
    try:
        sess_id = int(
            conn.execute(
                "INSERT INTO stream_sessions (streamer_id, title, started_at, status, created_at) VALUES (?, 'LEC Co-Stream', ?, 'recording', ?)",
                (streamer_id, dt_iso, dt_iso),
            ).lastrowid
        )
        chunk_id = int(
            conn.execute(
                "INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status, started_at, created_at) VALUES (?, ?, 1, ?, 'queued', ?, ?)",
                (sess_id, streamer_id, str(raw_file), dt_iso, dt_iso),
            ).lastrowid
        )
        conn.commit()
    finally:
        conn.close()

    class StubNeon:
        async def arefine_and_export_with_turbo(self, chunk_path, output_dir=None, **kwargs):
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            mp4 = _make_synthetic_mp4(out / "clip1.mp4")
            return {
                "ok": True,
                "clips": [
                    {
                        "index": 1,
                        "start": 5.0,
                        "end": 35.0,
                        "duration": 30.0,
                        "score": 95.0,
                        "title": "Hooked Highlight",
                        "hook": "Huge baron steal!",
                        "transcript": "Oh my god!",
                        "output_mp4": str(mp4),
                    }
                ],
            }

    class StubCopywriter:
        async def generate_youtube_copy(self, **kwargs):
            return {
                "title": "Caedrel: Baron Steal",
                "description": "Caedrel loses his mind over an unreal baron steal!",
                "tags": ["lec", "shorts"],
                "source": "llm",
            }

    class StubDrive:
        async def upload_clip(self, db, file_path, **kwargs):
            return {
                "drive_file_id": "drv_hook_1",
                "drive_url": "https://drive.google.com/file/d/drv_hook_1/view",
            }

    tg_requests: list[dict] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tg_requests.append(body)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 888, "chat": {"id": body["chat_id"]}}},
        )

    async def _run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(tg_handler)) as client:
            bot = TelegramBotService(settings, http_client=client)
            worker = ProcessingQueueWorker(
                settings,
                neon_client=StubNeon(),
                copywriter=StubCopywriter(),
                google_drive=StubDrive(),
                on_chunk_drafts_ready=bot.notify_chunk_ready,
            )
            return await worker.process_chunk(chunk_id)

    res = asyncio.run(_run())
    assert res["ok"] is True
    assert len(tg_requests) == 2
    assert "Caedrel: Baron Steal" in tg_requests[0]["text"]
    assert "Hook Channel" in tg_requests[0]["text"]

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, tg_chat_id, tg_message_id FROM clip_drafts WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        assert row["status"] == "notified"
        assert row["tg_chat_id"] == "-100999"
        assert row["tg_message_id"] == 888
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 8. FastAPI clips router endpoints & drafts_dir path containment
# ---------------------------------------------------------------------------


def test_clips_router_approve_reject_publish_publications_and_path_containment(
    phase4_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, tmp_path = phase4_env
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    acc = _seed_youtube_account(
        settings.db_path, settings, channel_id="UC_API", channel_title="API Shorts"
    )
    streamer_id = _seed_streamer_and_target(settings.db_path, account_id=acc, default_privacy="public")
    _, [clip1_id, clip2_id, clip3_id] = _seed_chunk_and_clips(
        settings, streamer_id=streamer_id, count=3, status="notified"
    )

    upload_seq = 0

    def yt_handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_seq
        if request.method == "POST":
            upload_seq += 1
            return httpx.Response(
                200, headers={"Location": f"https://upload.youtube.example/api/{upload_seq}"}
            )
        if request.method == "PUT":
            sid = str(request.url).rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": f"api_vid_{sid}"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def _run_client() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(yt_handler)) as yt_client:
            with TestClient(app) as client:
                app.state.youtube_publisher = YouTubePublisherService(
                    settings, http_client=yt_client, retry_base_delay=0.0
                )

                # Empty-body POST /approve works via Body(default=None) and publishes immediately
                ap_resp = client.post(f"/api/clips/{clip1_id}/approve")
                assert ap_resp.status_code == 200
                ap_body = ap_resp.json()
                assert ap_body["clip"]["status"] == "posted"
                assert len(ap_body["publications"]) == 1
                assert (
                    ap_body["publications"][0]["youtube_url"]
                    == "https://www.youtube.com/shorts/api_vid_1"
                )

                # POST /approve with publish_now=false marks approved without uploading
                ap_only = client.post(
                    f"/api/clips/{clip2_id}/approve",
                    json={"publish_now": False},
                )
                assert ap_only.status_code == 200
                assert ap_only.json()["clip"]["status"] == "approved"
                assert ap_only.json()["publications"] == []

                # POST /publish with privacy_status=unlisted publishes clip2
                pub_resp = client.post(
                    f"/api/clips/{clip2_id}/publish",
                    json={"privacy_status": "unlisted"},
                )
                assert pub_resp.status_code == 200
                pub_body = pub_resp.json()
                assert pub_body["clip"]["status"] == "posted"
                assert pub_body["publications"][0]["privacy_status"] == "unlisted"

                # GET /publications
                list_pubs = client.get(f"/api/clips/{clip2_id}/publications")
                assert list_pubs.status_code == 200
                assert list_pubs.json()["clip_id"] == clip2_id
                assert len(list_pubs.json()["items"]) == 1

                # POST /reject marks kept_local
                rj_resp = client.post(f"/api/clips/{clip3_id}/reject")
                assert rj_resp.status_code == 200
                assert rj_resp.json()["status"] == "kept_local"

                # Enum validation on PATCH /api/clips/{id} and POST /publish
                assert (
                    client.patch(f"/api/clips/{clip3_id}", json={"status": "bogus_state"}).status_code
                    == 422
                )
                assert (
                    client.post(f"/api/clips/{clip2_id}/publish", json={"privacy_status": "secret"}).status_code
                    == 422
                )
                assert client.post("/api/clips/99999/approve").status_code == 404
                assert client.post("/api/clips/99999/reject").status_code == 404
                assert client.get("/api/clips/99999/publications").status_code == 404

                # Path containment on /video, /thumbnail, /upload-drive, DELETE
                escaped_file = tmp_path / "escaped.mp4"
                escaped_file.write_bytes(b"escaped")
                conn = sqlite3.connect(settings.db_path)
                try:
                    conn.execute(
                        "UPDATE clip_drafts SET file_path = ?, thumbnail_path = ? WHERE id = ?",
                        (str(escaped_file), str(escaped_file), clip3_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

                assert client.get(f"/api/clips/{clip3_id}/video").status_code == 400
                assert client.get(f"/api/clips/{clip3_id}/thumbnail").status_code == 400
                assert client.post(f"/api/clips/{clip3_id}/upload-drive").status_code == 400
                assert client.delete(f"/api/clips/{clip3_id}").status_code == 400

    asyncio.run(_run_client())
