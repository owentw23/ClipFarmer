import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings
from app.db import init_db, open_connection, utcnow_iso
from app.security import encrypt_token, generate_fernet_key
from app.services.copywriter import CopywriterService, enforce_one_sentence
from app.services.google_drive import GoogleDriveService


@pytest.fixture()
def work_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="clip_phase3_copy_drive_") as tmp:
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
        "llm_base_url": "https://ark.ap-southeast.bytepluses.com/api/v3",
        "llm_model": "deepseek-v4-1-flash-261017",
        "llm_api_key": "test-llm-key",
    }
    base.update(overrides)
    return Settings(**base)


async def _seed_google_account(
    db_path: str,
    settings: Settings,
    *,
    channel_id: str = "UC_DRIVE_1",
    scopes: str = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/drive.file",
    status: str = "active",
    access_token: str = "valid-drive-access-token",
) -> int:
    now = utcnow_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    enc_access = encrypt_token(access_token, key=settings.token_encryption_key)
    enc_refresh = encrypt_token("refresh-tok", key=settings.token_encryption_key)
    conn = await open_connection(db_path)
    try:
        cur = await conn.execute(
            """
            INSERT INTO youtube_accounts (
                channel_id, channel_title, email,
                encrypted_refresh_token, encrypted_access_token,
                token_expires_at, scopes, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                f"Channel {channel_id}",
                "creator@example.com",
                enc_refresh,
                enc_access,
                expires_at,
                scopes,
                status,
                now,
                now,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)
    finally:
        await conn.close()


def test_enforce_one_sentence_cases() -> None:
    assert enforce_one_sentence("") == ""
    assert (
        enforce_one_sentence("First sentence! Second sentence. Third sentence?")
        == "First sentence!"
    )
    assert enforce_one_sentence("Line one\nline two.\nExtra line.") == "Line one line two."
    assert (
        enforce_one_sentence("Clutch 1v5 in 2.5 seconds! Chat goes wild.")
        == "Clutch 1v5 in 2.5 seconds!"
    )
    assert enforce_one_sentence("No ending punctuation") == "No ending punctuation."


def test_copywriter_valid_json_and_multi_sentence_trim(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    captured: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), json.loads(request.content.decode("utf-8"))))
        assert request.headers["Authorization"] == "Bearer test-llm-key"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "X" * 115,
                                    "description": "Tarik pulls off an unbelievable 1v5 clutch on Ascent!\nNobody in chat saw it coming. Subscribe for more.",
                                    "tags": ["#Valorant", "Shorts", "tarik", "VALORANT"],
                                }
                            )
                        }
                    }
                ]
            },
        )

    async def _run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            svc = CopywriterService(settings, http_client=client)
            return await svc.generate_youtube_copy(
                transcript="Wait what?! Oh my god he got all five!",
                neon_title="1v5 Clutch",
                neon_hook="Unbelievable 1v5",
                streamer_name="Tarik",
                stream_title="Radiant Ranked",
                platform="twitch",
            )

    result = asyncio.run(_run())
    assert len(captured) == 1
    assert captured[0][0] == "https://ark.ap-southeast.bytepluses.com/api/v3/chat/completions"
    assert captured[0][1]["model"] == "deepseek-v4-1-flash-261017"
    assert captured[0][1]["response_format"] == {"type": "json_object"}
    assert result["source"] == "llm"
    assert len(result["title"]) <= 100
    assert result["description"] == "Tarik pulls off an unbelievable 1v5 clutch on Ascent!"
    assert "\n" not in result["description"]
    assert result["tags"] == ["Valorant", "Shorts", "tarik"]


def test_copywriter_fenced_json_and_400_retry_without_response_format(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests_seen.append(body)
        if len(requests_seen) == 1:
            assert "response_format" in body
            return httpx.Response(400, json={"error": "response_format unsupported"})
        assert "response_format" not in body
        fenced = (
            "```json\n"
            '{"title": "Insane Ace On Mirage", '
            '"description": "He hits five headshots in six seconds. Pure aim.", '
            '"tags": ["CS2", "Ace"]}\n'
            "```"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})

    async def _run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            svc = CopywriterService(settings, http_client=client)
            return await svc.generate_youtube_copy(transcript="Five headshots!")

    result = asyncio.run(_run())
    assert len(requests_seen) == 2
    assert result == {
        "title": "Insane Ace On Mirage",
        "description": "He hits five headshots in six seconds.",
        "tags": ["CS2", "Ace"],
        "source": "llm",
    }


def test_copywriter_blocks_coding_plan_and_blank_key_uses_fallback(work_dir: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async def _run(settings: Settings) -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            svc = CopywriterService(settings, http_client=client)
            return await svc.generate_youtube_copy(
                transcript="Some transcript",
                neon_title="Mega Clutch Moment",
                neon_hook="Watch him wipe the whole squad! Then he laughs.",
                streamer_name="TenZ",
                stream_title="Morning Ranked",
                platform="twitch",
            )

    coding_settings = _make_settings(
        work_dir,
        llm_base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        llm_api_key="should-never-be-sent",
    )
    coding_res = asyncio.run(_run(coding_settings))
    assert calls == 0
    assert coding_res["source"] == "fallback"
    assert coding_res["title"] == "Mega Clutch Moment"
    assert coding_res["description"] == "TenZ (Morning Ranked) - Watch him wipe the whole squad!"
    assert "\n" not in coding_res["description"]

    blank_key_settings = _make_settings(work_dir, llm_api_key="   ")
    blank_res = asyncio.run(_run(blank_key_settings))
    assert calls == 0
    assert blank_res["source"] == "fallback"
    assert blank_res["title"] == "Mega Clutch Moment"
    assert blank_res["description"] == "TenZ (Morning Ranked) - Watch him wipe the whole squad!"


def test_google_drive_folder_cache_upload_permission_and_delete(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    asyncio.run(init_db(settings.db_path))
    account_id = asyncio.run(_seed_google_account(settings.db_path, settings))

    clip_path = work_dir / "tarik_2026-11-22_120000_chunk01_draft_1.mp4"
    clip_bytes = b"fake-mp4-video-payload-1234567890"
    clip_path.write_bytes(clip_bytes)

    state = {
        "folder_queries": [],
        "folder_creates": [],
        "upload_inits": [],
        "upload_puts": [],
        "permissions": [],
        "deletes": [],
    }
    folders_by_key: dict[tuple[str, str], str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        parsed = urlparse(url)

        if request.method == "GET" and parsed.path == "/drive/v3/files":
            qs = parse_qs(parsed.query)
            q = qs["q"][0]
            state["folder_queries"].append(q)
            return httpx.Response(200, json={"files": []})

        if request.method == "POST" and parsed.path == "/drive/v3/files":
            body = json.loads(request.content.decode("utf-8"))
            parent = body["parents"][0]
            name = body["name"]
            folder_id = f"folder_{len(state['folder_creates']) + 1}"
            folders_by_key[(parent, name)] = folder_id
            state["folder_creates"].append((parent, name, folder_id))
            return httpx.Response(200, json={"id": folder_id})

        if request.method == "POST" and parsed.path == "/upload/drive/v3/files":
            body = json.loads(request.content.decode("utf-8"))
            state["upload_inits"].append(
                {
                    "query": parsed.query,
                    "body": body,
                    "x_len": request.headers.get("X-Upload-Content-Length"),
                }
            )
            return httpx.Response(
                200,
                headers={"Location": "https://upload.example.invalid/session/abc123"},
            )

        if request.method == "PUT" and url == "https://upload.example.invalid/session/abc123":
            state["upload_puts"].append(
                {
                    "content_range": request.headers.get("Content-Range"),
                    "bytes": request.content,
                }
            )
            return httpx.Response(
                200,
                json={
                    "id": "drive_file_999",
                    "webViewLink": "https://drive.google.com/file/d/drive_file_999/view?usp=drivesdk",
                },
            )

        if request.method == "POST" and parsed.path == "/drive/v3/files/drive_file_999/permissions":
            body = json.loads(request.content.decode("utf-8"))
            state["permissions"].append(body)
            return httpx.Response(200, json={"id": "perm_anyone"})

        if request.method == "DELETE" and parsed.path.startswith("/drive/v3/files/"):
            file_id = parsed.path.rsplit("/", 1)[-1]
            state["deletes"].append(file_id)
            if file_id == "drive_file_999":
                return httpx.Response(204)
            if file_id == "missing_file":
                return httpx.Response(404)
            return httpx.Response(500, json={"error": "server error"})

        raise AssertionError(f"Unexpected request: {request.method} {url}")

    async def _run() -> None:
        conn = await open_connection(settings.db_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                svc = GoogleDriveService(settings, http_client=client)

                # First upload creates 3 folders: ClipPipeline -> O'Malley -> 2026-11-22
                first = await svc.upload_clip(
                    conn,
                    clip_path,
                    streamer_name="O'Malley",
                    clip_date="2026-11-22",
                    display_name="Best Clip.mp4",
                    account_id=account_id,
                    share_anyone=True,
                )
                assert first == {
                    "drive_file_id": "drive_file_999",
                    "drive_url": "https://drive.google.com/file/d/drive_file_999/view?usp=drivesdk",
                }
                assert len(state["folder_queries"]) == 3
                assert r"O\'Malley" in state["folder_queries"][1]
                assert [c[1] for c in state["folder_creates"]] == [
                    "ClipPipeline",
                    "O'Malley",
                    "2026-11-22",
                ]
                assert state["upload_inits"][0]["body"] == {
                    "name": "Best Clip.mp4",
                    "parents": ["folder_3"],
                }
                assert state["upload_inits"][0]["x_len"] == str(len(clip_bytes))
                assert len(state["upload_puts"]) == 1
                assert (
                    state["upload_puts"][0]["content_range"]
                    == f"bytes 0-{len(clip_bytes) - 1}/{len(clip_bytes)}"
                )
                assert state["upload_puts"][0]["bytes"] == clip_bytes
                assert state["permissions"] == [{"role": "reader", "type": "anyone"}]

                # Second upload with same streamer/date hits in-memory folder cache (0 extra folder GET/POSTs)
                second = await svc.upload_clip(
                    conn,
                    clip_path,
                    streamer_name="O'Malley",
                    clip_date="2026-11-22",
                )
                assert second is not None
                assert len(state["folder_queries"]) == 3
                assert len(state["folder_creates"]) == 3

                # Delete 204, 404, and 500
                assert await svc.delete_file(conn, "drive_file_999") is True
                assert await svc.delete_file(conn, "missing_file") is True
                assert await svc.delete_file(conn, "broken_file") is False
        finally:
            await conn.close()

    asyncio.run(_run())


def test_google_drive_graceful_none_when_no_drive_account_exists(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    asyncio.run(init_db(settings.db_path))
    # Seed only an account WITHOUT drive.file scope
    asyncio.run(
        _seed_google_account(
            settings.db_path,
            settings,
            channel_id="UC_NO_DRIVE",
            scopes="https://www.googleapis.com/auth/youtube.upload",
        )
    )

    clip_path = work_dir / "clip.mp4"
    clip_path.write_bytes(b"video")

    async def _run() -> None:
        conn = await open_connection(settings.db_path)
        try:
            svc = GoogleDriveService(settings)
            assert await svc.resolve_drive_account_id(conn) is None
            assert (
                await svc.upload_clip(
                    conn,
                    clip_path,
                    streamer_name="Tarik",
                    clip_date="2026-11-22",
                )
                is None
            )
            assert await svc.delete_file(conn, "any_id") is False
        finally:
            await conn.close()

    asyncio.run(_run())


def test_copywriter_http_error_and_bad_json_fallback(work_dir: Path) -> None:
    settings = _make_settings(work_dir)
    mode = {"case": "http_500"}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode["case"] == "http_500":
            return httpx.Response(500, json={"error": "upstream down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json at all"}}]})

    async def _run() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            svc = CopywriterService(settings, http_client=client)
            return await svc.generate_youtube_copy(
                transcript="Fallback transcript",
                neon_title="Clutch Play",
                neon_hook="Insane play by Shroud! Second sentence ignored.",
                streamer_name="Shroud",
                stream_title="Apex",
                platform="twitch",
            )

    res_500 = asyncio.run(_run())
    assert res_500["source"] == "fallback"
    assert res_500["description"] == "Shroud (Apex) - Insane play by Shroud!"

    mode["case"] = "bad_json"
    res_bad = asyncio.run(_run())
    assert res_bad["source"] == "fallback"
    assert res_bad["title"] == "Clutch Play"


def test_google_drive_multichunk_upload_and_account_fallback(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.google_drive as drive_mod

    monkeypatch.setattr(drive_mod, "RESUMABLE_CHUNK_SIZE", 10)
    settings = _make_settings(work_dir)
    asyncio.run(init_db(settings.db_path))
    no_drive_id = asyncio.run(
        _seed_google_account(
            settings.db_path,
            settings,
            channel_id="UC_YT_ONLY",
            scopes="https://www.googleapis.com/auth/youtube.upload",
        )
    )
    drive_id = asyncio.run(
        _seed_google_account(
            settings.db_path,
            settings,
            channel_id="UC_WITH_DRIVE",
            scopes="https://www.googleapis.com/auth/drive.file",
        )
    )

    clip_path = work_dir / "multichunk.mp4"
    payload = b"0123456789abcdefghijklmno"  # 25 bytes -> 10, 10, 5
    clip_path.write_bytes(payload)

    ranges: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        if request.method == "GET" and parsed.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [{"id": "existing_folder", "name": "x"}]})
        if request.method == "POST" and parsed.path == "/upload/drive/v3/files":
            return httpx.Response(200, headers={"Location": "https://upload.example.invalid/chunked"})
        if request.method == "PUT" and str(request.url) == "https://upload.example.invalid/chunked":
            ranges.append(request.headers["Content-Range"])
            if len(ranges) < 3:
                return httpx.Response(308)
            return httpx.Response(200, json={"id": "chunked_file_1"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def _run() -> None:
        conn = await open_connection(settings.db_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                svc = GoogleDriveService(settings, http_client=client)
                # Preferred account lacks drive.file, so resolver falls back to drive_id
                assert await svc.resolve_drive_account_id(conn, preferred_account_id=no_drive_id) == drive_id
                result = await svc.upload_clip(
                    conn,
                    clip_path,
                    streamer_name="Tarik",
                    clip_date="2026-11-22",
                    account_id=no_drive_id,
                    share_anyone=False,
                )
                assert result == {
                    "drive_file_id": "chunked_file_1",
                    "drive_url": "https://drive.google.com/file/d/chunked_file_1/view",
                }
                assert ranges == [
                    "bytes 0-9/25",
                    "bytes 10-19/25",
                    "bytes 20-24/25",
                ]
        finally:
            await conn.close()

    asyncio.run(_run())


def test_dedicated_google_drive_oauth_and_refresh_credentials(work_dir: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.routers import accounts as accounts_router
    from app.routers import auth as auth_router
    from app.services.google_oauth import get_valid_access_token

    blank_settings = _make_settings(
        work_dir,
        google_client_id="yt-client-id",
        google_client_secret="yt-client-secret",
        google_drive_client_id="",
        google_drive_client_secret="",
    )
    asyncio.run(init_db(blank_settings.db_path))

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_settings] = lambda: blank_settings

    with TestClient(app) as client:
        unconfigured = client.get("/api/auth/google-drive/start")
        assert unconfigured.status_code == 400

    drive_settings = _make_settings(
        work_dir,
        google_client_id="yt-client-id",
        google_client_secret="yt-client-secret",
        google_drive_client_id="drive-5tb-client-id",
        google_drive_client_secret="drive-5tb-client-secret",
        google_drive_redirect_uri="http://127.0.0.1:8000/api/auth/google-drive/callback",
    )

    seen_token_forms: list[dict[str, list[str]]] = []
    seen_yt_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://oauth2.googleapis.com/token":
            form = parse_qs(request.content.decode("utf-8"))
            seen_token_forms.append(form)
            if form.get("grant_type") == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "drive-access-1",
                        "refresh_token": "drive-refresh-1",
                        "expires_in": 3600,
                        "scope": "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email",
                    },
                )
            if form.get("grant_type") == ["refresh_token"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "drive-access-refreshed",
                        "expires_in": 3600,
                    },
                )
        if url == "https://www.googleapis.com/oauth2/v2/userinfo":
            return httpx.Response(
                200,
                json={"email": "vault5tb@gmail.com", "id": "user-5tb-1"},
            )
        if "youtube/v3/channels" in url:
            seen_yt_calls.append(url)
            return httpx.Response(500)
        raise AssertionError(f"Unexpected request: {request.method} {url}")

    async def _mock_client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c

    app.dependency_overrides[get_settings] = lambda: drive_settings
    app.dependency_overrides[auth_router.get_oauth_http_client] = _mock_client

    with TestClient(app) as client:
        start_resp = client.get("/api/auth/google-drive/start")
        assert start_resp.status_code == 200
        start_data = start_resp.json()
        assert start_data["configured"] is True
        qs = parse_qs(urlparse(start_data["auth_url"]).query)
        assert qs["client_id"] == ["drive-5tb-client-id"]
        assert qs["redirect_uri"] == ["http://127.0.0.1:8000/api/auth/google-drive/callback"]
        assert "https://www.googleapis.com/auth/drive.file" in qs["scope"][0]

        cb_resp = client.get(
            "/api/auth/google-drive/callback",
            params={"code": "drive-code-1", "state": start_data["state"], "format": "json"},
        )
        assert cb_resp.status_code == 200
        cb_data = cb_resp.json()
        assert cb_data["ok"] is True
        assert len(cb_data["accounts"]) == 1
        drive_acc = cb_data["accounts"][0]
        assert drive_acc["provider"] == "google_drive"
        assert drive_acc["oauth_provider"] == "google_drive"
        assert drive_acc["channel_id"] == "drive-only:vault5tb@gmail.com"
        assert drive_acc["email"] == "vault5tb@gmail.com"
        assert seen_yt_calls == []
        assert seen_token_forms[0]["client_id"] == ["drive-5tb-client-id"]
        assert seen_token_forms[0]["client_secret"] == ["drive-5tb-client-secret"]

        list_resp = client.get("/api/accounts")
        assert list_resp.status_code == 200
        listed = list_resp.json()["items"]
        assert len(listed) == 1
        assert listed[0]["provider"] == "google_drive"

    # Seed a separate YouTube account with drive.file and verify resolver prioritizes google_drive
    # and get_valid_access_token refreshes using GOOGLE_DRIVE_CLIENT_ID/SECRET.
    async def _verify_priority_and_refresh() -> None:
        conn = await open_connection(drive_settings.db_path)
        try:
            # Insert a standard google YouTube account with a lower id or alongside drive_acc
            now = utcnow_iso()
            await conn.execute(
                """
                INSERT INTO youtube_accounts (
                    channel_id, channel_title, email, oauth_provider,
                    encrypted_refresh_token, encrypted_access_token,
                    token_expires_at, scopes, status, created_at, updated_at
                )
                VALUES (?, ?, ?, 'google', ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    "UC_MAIN_YT",
                    "Main YT",
                    "yt@gmail.com",
                    encrypt_token("yt-ref", key=drive_settings.token_encryption_key),
                    encrypt_token("yt-acc", key=drive_settings.token_encryption_key),
                    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/drive.file",
                    now,
                    now,
                ),
            )
            # Force the dedicated drive account's token to appear expired so refresh triggers
            expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            await conn.execute(
                "UPDATE youtube_accounts SET token_expires_at = ? WHERE id = ?",
                (expired, drive_acc["id"]),
            )
            await conn.commit()

            svc = GoogleDriveService(drive_settings)
            picked_id = await svc.resolve_drive_account_id(conn)
            assert picked_id == drive_acc["id"]

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                refreshed_token = await get_valid_access_token(
                    conn, drive_settings, drive_acc["id"], http_client=client
                )
                assert refreshed_token == "drive-access-refreshed"
                assert seen_token_forms[-1]["client_id"] == ["drive-5tb-client-id"]
                assert seen_token_forms[-1]["client_secret"] == ["drive-5tb-client-secret"]
        finally:
            await conn.close()

    asyncio.run(_verify_priority_and_refresh())

