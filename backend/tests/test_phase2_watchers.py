import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings
from app.db import init_db, utcnow_iso
from app.services.twitch_watcher import TwitchEventSubClient, TwitchWatcher
from app.services.watcher_manager import LiveStreamInfo, WatcherManager
from app.services.youtube_watcher import YouTubeWatcher


@pytest.fixture()
def work_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="clip_phase2_watchers_") as tmp:
        yield Path(tmp)


def test_twitch_batched_helix_splits_over_100_logins() -> None:
    helix_batches: list[list[str]] = []
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if str(request.url) == "https://id.twitch.tv/oauth2/token":
            token_calls += 1
            return httpx.Response(
                200,
                json={"access_token": "app_tok_1", "expires_in": 3600, "token_type": "bearer"},
            )
        if request.url.path == "/helix/streams":
            assert request.headers["Client-Id"] == "cid"
            assert request.headers["Authorization"] == "Bearer app_tok_1"
            logins = request.url.params.get_list("user_login")
            helix_batches.append(logins)
            data = []
            if "streamer_001" in logins:
                data.append(
                    {
                        "id": "stream_1",
                        "user_login": "streamer_001",
                        "title": "Ranked Grind",
                        "started_at": "2026-11-22T10:00:00Z",
                        "viewer_count": 1234,
                    }
                )
            if "streamer_120" in logins:
                data.append(
                    {
                        "id": "stream_120",
                        "user_login": "streamer_120",
                        "title": "Late Night Stream",
                        "started_at": "2026-11-22T11:00:00Z",
                        "viewer_count": 55,
                    }
                )
            return httpx.Response(200, json={"data": data})
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            watcher = TwitchWatcher(client_id="cid", client_secret="csec", http_client=client)
            logins = [f"streamer_{i:03d}" for i in range(125)]
            live = await watcher.check_channels(logins)

        assert token_calls == 1
        assert len(helix_batches) == 2
        assert len(helix_batches[0]) == 100
        assert len(helix_batches[1]) == 25
        assert set(live.keys()) == {"streamer_001", "streamer_120"}
        assert live["streamer_001"]["external_stream_id"] == "stream_1"
        assert live["streamer_001"]["stream_url"] == "https://www.twitch.tv/streamer_001"
        assert live["streamer_120"]["viewer_count"] == 55

    asyncio.run(run())


def test_twitch_401_refreshes_token_and_retries() -> None:
    token_seq = ["expired_token", "fresh_token"]
    seen_auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://id.twitch.tv/oauth2/token":
            tok = token_seq.pop(0)
            return httpx.Response(200, json={"access_token": tok, "expires_in": 3600})
        if request.url.path == "/helix/streams":
            auth = request.headers["Authorization"]
            seen_auth_headers.append(auth)
            if auth == "Bearer expired_token":
                return httpx.Response(401, json={"error": "Unauthorized", "status": 401})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "live_99",
                            "user_login": "tarik",
                            "title": "Valo Watchparty",
                            "started_at": "2026-11-22T12:00:00Z",
                            "viewer_count": 42000,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            watcher = TwitchWatcher(client_id="cid", client_secret="csec", http_client=client)
            live = await watcher.check_channels(["Tarik"])

        assert seen_auth_headers == ["Bearer expired_token", "Bearer fresh_token"]
        assert "tarik" in live
        assert live["tarik"]["external_stream_id"] == "live_99"
        assert live["tarik"]["title"] == "Valo Watchparty"

    asyncio.run(run())


def test_twitch_429_rate_limit_handled_gracefully() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if str(request.url) == "https://id.twitch.tv/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok_429", "expires_in": 3600})
        if request.url.path == "/helix/streams":
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "stream_ok",
                            "user_login": "caedrel",
                            "title": "Worlds",
                            "started_at": "2026-11-22T12:00:00Z",
                            "viewer_count": 9000,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            watcher = TwitchWatcher(
                client_id="cid",
                client_secret="csec",
                http_client=client,
                max_rate_limit_wait=0.1,
            )
            live = await watcher.check_channels(["caedrel"])

        assert calls == 2
        assert live["caedrel"]["external_stream_id"] == "stream_ok"

    asyncio.run(run())


def test_twitch_streamlink_fallback_when_creds_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    class FakeProc:
        def __init__(self, rc: int, payload: dict) -> None:
            self.returncode = rc
            self._stdout = json.dumps(payload).encode("utf-8")

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._stdout, b""

    async def fake_exec(*args: str, **kwargs) -> FakeProc:
        calls.append(args)
        url = args[-1]
        if url.endswith("/tarik"):
            return FakeProc(
                0,
                {
                    "metadata": {"id": "sl_123", "title": "Tarik Live"},
                    "streams": {"best": {"url": "https://video.example/best.m3u8"}},
                },
            )
        return FakeProc(1, {"error": "No playable streams found"})

    monkeypatch.setattr("app.services.twitch_watcher._resolve_executable", lambda name: "streamlink")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def run() -> None:
        watcher = TwitchWatcher(client_id="", client_secret="")
        live = await watcher.check_channels(["tarik", "offline_user"])

        assert set(live.keys()) == {"tarik"}
        assert live["tarik"]["external_stream_id"] == "sl_123"
        assert live["tarik"]["title"] == "Tarik Live"
        assert live["tarik"]["stream_url"] == "https://www.twitch.tv/tarik"
        assert len(calls) == 2
        assert all("--json" in cmd for cmd in calls)

    asyncio.run(run())


def test_twitch_eventsub_notification_dispatches_callbacks() -> None:
    events: list[tuple[str, dict]] = []

    async def on_on(ev: dict) -> None:
        events.append(("online", ev))

    async def on_off(ev: dict) -> None:
        events.append(("offline", ev))

    async def run() -> None:
        client = TwitchEventSubClient(
            client_id="cid",
            client_secret="csec",
            on_online=on_on,
            on_offline=on_off,
        )
        await client.handle_message(
            {
                "metadata": {"message_type": "session_welcome"},
                "payload": {"session": {"id": "ws_sess_1"}},
            }
        )
        assert client.connected is True
        assert client.session_id == "ws_sess_1"

        await client.handle_message(
            {
                "metadata": {"message_type": "notification"},
                "payload": {
                    "subscription": {"type": "stream.online"},
                    "event": {"broadcaster_user_login": "tarik"},
                },
            }
        )
        await client.handle_message(
            {
                "metadata": {"message_type": "notification"},
                "payload": {
                    "subscription": {"type": "stream.offline"},
                    "event": {"broadcaster_user_login": "tarik"},
                },
            }
        )
        assert events == [
            ("online", {"broadcaster_user_login": "tarik"}),
            ("offline", {"broadcaster_user_login": "tarik"}),
        ]

    asyncio.run(run())


def test_youtube_live_url_builder_and_html_parsing() -> None:
    assert YouTubeWatcher.build_live_url("@ludwig") == "https://www.youtube.com/@ludwig/live"
    assert (
        YouTubeWatcher.build_live_url("UC1234567890abcdef")
        == "https://www.youtube.com/channel/UC1234567890abcdef/live"
    )

    live_html = """
    <html><head>
      <link rel="canonical" href="https://www.youtube.com/watch?v=abc123XYZ00">
      <title>Live Finals Match! - YouTube</title>
    </head>
    <body><script>
      var ytInitialPlayerResponse = {"videoDetails":{"videoId":"abc123XYZ00","title":"Live Finals Match!","isLive":true}};
    </script></body></html>
    """
    offline_html = """
    <html><head>
      <link rel="canonical" href="https://www.youtube.com/@ludwig">
      <title>Ludwig - YouTube</title>
    </head><body>{"isLive":false}</body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/@ludwig/live":
            return httpx.Response(200, text=live_html)
        if request.url.path == "/channel/UC1234567890abcdef/live":
            return httpx.Response(200, text=offline_html)
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            yt = YouTubeWatcher(http_client=client)
            live_res = await yt.check_channel("@ludwig")
            off_res = await yt.check_channel("UC1234567890abcdef")

        assert live_res == {
            "external_stream_id": "abc123XYZ00",
            "title": "Live Finals Match!",
            "stream_url": "https://www.youtube.com/watch?v=abc123XYZ00",
        }
        assert off_res is None

    asyncio.run(run())


def test_youtube_confirm_with_ytdlp_mocked(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_cmds: list[tuple[str, ...]] = []
    cookies = work_dir / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    class FakeProc:
        def __init__(self) -> None:
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            payload = {
                "id": "yt_vid_777",
                "title": "Championship Stream",
                "is_live": True,
                "webpage_url": "https://www.youtube.com/watch?v=yt_vid_777",
            }
            return json.dumps(payload).encode("utf-8"), b""

    async def fake_exec(*args: str, **kwargs) -> FakeProc:
        captured_cmds.append(args)
        return FakeProc()

    monkeypatch.setattr("app.services.youtube_watcher._resolve_executable", lambda name: "yt-dlp")
    monkeypatch.setattr("app.services.youtube_watcher.shutil.which", lambda name: "C:/node/node.exe")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def run() -> None:
        yt = YouTubeWatcher(cookies_file=str(cookies))
        confirmed = await yt.confirm_with_ytdlp("https://www.youtube.com/@ludwig/live")
        assert confirmed == {
            "is_live": True,
            "id": "yt_vid_777",
            "title": "Championship Stream",
            "stream_url": "https://www.youtube.com/watch?v=yt_vid_777",
        }
        cmd = captured_cmds[0]
        assert "--dump-single-json" in cmd
        assert "--no-playlist" in cmd
        assert "--js-runtimes" in cmd
        assert "node:C:/node/node.exe" in cmd
        assert "--cookies" in cmd
        assert str(cookies) in cmd

    asyncio.run(run())


def test_watcher_manager_poll_once_and_check_streamer_transitions(work_dir: Path) -> None:
    db_file = work_dir / "watcher_test.sqlite3"
    asyncio.run(init_db(db_file))
    now = utcnow_iso()

    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Tarik", "tarik", "twitch", "https://twitch.tv/tarik", "tarik", 1, "{}", 0, now, now),
        )
        conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Ludwig",
                "ludwig",
                "youtube",
                "https://www.youtube.com/@ludwig",
                "@ludwig",
                1,
                "{}",
                0,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Ignored",
                "ignored",
                "twitch",
                "https://twitch.tv/ignored",
                "ignored",
                0,
                "{}",
                0,
                now,
                now,
            ),
        )
        conn.commit()
        rows = conn.execute("SELECT id, slug FROM streamers ORDER BY id ASC").fetchall()
        id_by_slug = {slug: sid for sid, slug in rows}
    finally:
        conn.close()

    state = {
        "twitch_live": True,
        "yt_live": False,
    }

    class StubTwitchWatcher:
        async def check_channels(self, logins: list[str]) -> dict[str, dict]:
            assert "ignored" not in logins
            if state["twitch_live"] and "tarik" in logins:
                return {
                    "tarik": {
                        "external_stream_id": "tw_1",
                        "title": "Tarik Live",
                        "stream_url": "https://www.twitch.tv/tarik",
                        "started_at": "2026-11-22T10:00:00Z",
                        "viewer_count": 5000,
                    }
                }
            return {}

    class StubYouTubeWatcher:
        async def check_channel(self, channel_ref: str) -> dict | None:
            if state["yt_live"] and channel_ref == "@ludwig":
                return {
                    "external_stream_id": "yt_1",
                    "title": "Ludwig Live",
                    "stream_url": "https://www.youtube.com/watch?v=yt_1",
                }
            return None

    online_events: list[LiveStreamInfo] = []
    offline_events: list[int] = []

    async def on_online(info: LiveStreamInfo) -> None:
        online_events.append(info)

    async def on_offline(streamer_id: int) -> None:
        offline_events.append(streamer_id)

    settings = Settings(db_path=str(db_file))
    manager = WatcherManager(
        settings=settings,
        on_online=on_online,
        on_offline=on_offline,
        twitch_watcher=StubTwitchWatcher(),  # type: ignore[arg-type]
        youtube_watcher=StubYouTubeWatcher(),  # type: ignore[arg-type]
        youtube_stagger_sec=0.0,
    )

    async def run() -> None:
        # Poll 1: Tarik transitions offline -> online; Ludwig stays offline
        live1 = await manager.poll_once()
        assert len(live1) == 1
        assert live1[0].streamer_slug == "tarik"
        assert len(online_events) == 1
        assert online_events[0].external_stream_id == "tw_1"
        assert offline_events == []

        # Poll 2: Tarik stays online (no duplicate callback), Ludwig transitions offline -> online
        state["yt_live"] = True
        live2 = await manager.poll_once()
        assert {item.streamer_slug for item in live2} == {"tarik", "ludwig"}
        assert [ev.streamer_slug for ev in online_events] == ["tarik", "ludwig"]
        assert offline_events == []

        # Single-streamer check: Tarik goes offline -> online->offline callback
        state["twitch_live"] = False
        check_res = await manager.check_streamer(id_by_slug["tarik"])
        assert check_res["streamer_id"] == id_by_slug["tarik"]
        assert check_res["is_live"] is False
        assert check_res["stream"] is None
        assert isinstance(check_res["checked_at"], str)
        assert offline_events == [id_by_slug["tarik"]]

        # Missing streamer raises LookupError
        with pytest.raises(LookupError):
            await manager.check_streamer(99999)

        # Background loop start / stop lifecycle
        await manager.start()
        await asyncio.sleep(0.05)
        await manager.stop()

    asyncio.run(run())

    conn = sqlite3.connect(db_file)
    try:
        db_rows = conn.execute(
            "SELECT slug, is_live, last_checked_at FROM streamers ORDER BY id ASC"
        ).fetchall()
        by_slug = {slug: (is_live, checked_at) for slug, is_live, checked_at in db_rows}
        assert by_slug["tarik"][0] == 0
        assert by_slug["tarik"][1] is not None
        assert by_slug["ludwig"][0] == 1
        assert by_slug["ludwig"][1] is not None
        assert by_slug["ignored"] == (0, None)
    finally:
        conn.close()
