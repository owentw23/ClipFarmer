import asyncio
from datetime import datetime, timezone
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
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, utcnow_iso
from app.main import create_app
from app.services.neon_client import NeonClippingClient
from app.services.processing_queue import ProcessingQueueWorker
from app.services.twitch_watcher import TwitchWatcher
from app.services.watcher_manager import WatcherManager
from app.services.youtube_watcher import YouTubeWatcher
from app.storage import ensure_storage_dirs, raw_chunk_filename, streamer_preview_path


def _make_synthetic_mp4(dest: Path) -> Path:
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
        "testsrc=size=640x360:rate=15:duration=2",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=stereo",
        "-t",
        "2",
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


def test_watcher_ignores_probe_timeouts_skips_active_fallback_and_debounces_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  with tempfile.TemporaryDirectory(prefix="clip_watch_fix_") as tmp_dir:
    tmp_path = Path(tmp_dir)
    db_file = tmp_path / "watcher_resilience.sqlite3"
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
            VALUES
                ('Lacy', 'lacy', 'twitch', 'https://twitch.tv/lacy', 'lacy', 1, '{}', 1, ?, ?),
                ('Ludwig', 'ludwig', 'youtube', 'https://youtube.com/@ludwig', '@ludwig', 1, '{}', 1, ?, ?)
            """,
            (now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    settings = Settings(db_path=str(db_file), enable_background_workers=False)

    # 1) Simulate a streamlink --json timeout: subprocess must be killed and error tracked
    killed = False

    class HangingProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(5)
            return b"", b""

        def kill(self):
            nonlocal killed
            killed = True
            self.returncode = -9

        async def wait(self):
            return -9

    async def fake_exec(*cmd, **kwargs):
        return HangingProc()

    import app.services.twitch_watcher as tw_mod

    monkeypatch.setattr(tw_mod, "_resolve_executable", lambda _name: "streamlink")
    monkeypatch.setattr(tw_mod, "create_subprocess_exec", fake_exec)

    orig_wait_for = asyncio.wait_for

    async def fast_timeout(coro, timeout=None):
        if timeout == 30.0:
            return await orig_wait_for(coro, timeout=0.05)
        return await orig_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", fast_timeout)

    offline_events: list[int] = []
    recording_ids = {2}  # Ludwig recording; Lacy NOT recording so timeout path executes

    tw = TwitchWatcher(client_id="", client_secret="")
    yt = YouTubeWatcher(
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _req: (_ for _ in ()).throw(httpx.ConnectTimeout("timeout")))
        )
    )
    wm = WatcherManager(
        settings,
        on_offline=offline_events.append,
        twitch_watcher=tw,
        youtube_watcher=yt,
        youtube_stagger_sec=0.0,
        is_recording=lambda sid: sid in recording_ids,
        offline_confirm_polls=2,
    )

    asyncio.run(wm.poll_once())
    assert killed is True
    assert "lacy" in tw.last_probe_errors
    assert "@ludwig" in yt.last_probe_errors
    assert offline_events == []  # Neither streamer falsely marked offline on timeout!

    # 2) When Lacy IS actively recording with streamlink fallback, poll skips redundant probe
    recording_ids.add(1)
    probed_logins: list[str] = []

    async def spy_check(logins):
        probed_logins.extend(logins)
        tw.last_probe_errors = set()
        return {}

    tw.check_channels = spy_check  # type: ignore[method-assign]

    # YouTube returns confirmed offline HTML (200 OK without isLive) -> requires 2 strikes!
    yt_offline = YouTubeWatcher(
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _req: httpx.Response(200, text="<html>offline</html>"))
        )
    )
    wm.youtube_watcher = yt_offline

    asyncio.run(wm.poll_once())
    assert probed_logins == []  # Active Twitch fallback recording skipped redundant probe
    assert offline_events == []  # Strike 1/2 for Ludwig — not stopped yet!

    asyncio.run(wm.poll_once())
    assert offline_events == [2]  # Strike 2/2 confirmed -> Ludwig stopped cleanly


def test_facecam_detect_borderless_returns_landscape_median_box() -> None:
    engine_dir = REPO_ROOT / "engines" / "neonclipping"
    if not (engine_dir / "facecam_detect.py").is_file():
        pytest.skip("NeonClipping engine not installed")

    # Import using numpy/facecam_detect in engine dir or check via engine venv
    engine_py = engine_dir / ".venv" / "Scripts" / "python.exe"
    if not engine_py.is_file():
        pytest.skip("NeonClipping venv not installed")

    script = r"""
import json, sys
import numpy as np
sys.path.insert(0, '.')
import facecam_detect

class StubFaces:
    def find(self, img):
        # Simulate a small green-screen streamer face (26x34) in bottom-right corner on 640x360
        return [(530.0, 235.0, 26.0, 34.0, 0.91)]

frames = np.zeros((14, facecam_detect.BH, facecam_detect.BW, 3), dtype=np.uint8)
res = facecam_detect.borderless_face(frames, StubFaces())
assert res is not None, "Expected borderless_face to detect small green-screen face"
rate, (x, y, w, h) = res
px_w = w * facecam_detect.BW
px_h = h * facecam_detect.BH
ar = px_w / px_h
print(json.dumps({"rate": rate, "x": x, "y": y, "w": w, "h": h, "ar": ar}))
assert 1.65 <= ar <= 1.85, f"Expected landscape ~1.75:1 box, got {ar:.2f}"
assert 0.0 <= x and x + w <= 1.0001
assert 0.0 <= y and y + h <= 1.0001
"""
    proc = subprocess.run(
        [str(engine_py), "-c", script],
        cwd=str(engine_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"borderless_face check failed:\n{proc.stdout}\n{proc.stderr}"


def test_streamer_preview_endpoints_and_cam_box_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="clip_cam_test_") as tmp:
        root = Path(tmp) / "Clips_Store"
        settings = Settings(
            clips_store_root=str(root),
            raw_recordings_dir=str(root / "raw recordings"),
            drafts_dir=str(root / "drafts"),
            neonclipping_data_dir=str(root / "neon_tmp"),
            db_path=str(root / "db" / "pipeline.sqlite3"),
            enable_background_workers=False,
        )
        ensure_storage_dirs(settings)
        asyncio.run(init_db(settings.db_path))

        monkeypatch.setattr("app.main.get_settings", lambda: settings)
        app = create_app()
        app.dependency_overrides[get_settings] = lambda: settings

        with TestClient(app) as client:
            created = client.post(
                "/api/streamers",
                json={
                    "name": "GreenScreenPro",
                    "platform": "twitch",
                    "channel_url": "https://twitch.tv/greenscreenpro",
                },
            )
            assert created.status_code == 201
            streamer = created.json()
            sid = int(streamer["id"])
            assert streamer["has_preview"] is False

            # No screenshot yet -> 404
            assert client.get(f"/api/streamers/{sid}/preview").status_code == 404

            # Seed a synthetic recording chunk so /preview/refresh extracts a JPEG
            mp4 = _make_synthetic_mp4(
                Path(settings.raw_recordings_dir)
                / raw_chunk_filename("greenscreenpro", datetime.now(timezone.utc), 1)
            )
            now = utcnow_iso()
            conn = sqlite3.connect(settings.db_path)
            try:
                sess_id = int(
                    conn.execute(
                        "INSERT INTO stream_sessions (streamer_id, title, started_at, status, created_at) VALUES (?, 'Test', ?, 'completed', ?)",
                        (sid, now, now),
                    ).lastrowid
                )
                conn.execute(
                    "INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status, started_at, created_at) VALUES (?, ?, 1, ?, 'processed', ?, ?)",
                    (sess_id, sid, str(mp4), now, now),
                )
                conn.commit()
            finally:
                conn.close()

            refreshed = client.post(f"/api/streamers/{sid}/preview/refresh")
            assert refreshed.status_code == 200
            r_body = refreshed.json()
            assert r_body["has_preview"] is True
            assert r_body["preview_updated_at"] is not None

            img = client.get(f"/api/streamers/{sid}/preview")
            assert img.status_code == 200
            assert img.headers["content-type"].startswith("image/jpeg")
            assert len(img.content) > 500

            # Save valid custom cam_box
            patched = client.patch(
                f"/api/streamers/{sid}",
                json={
                    "clip_opts": {
                        "cam_auto": False,
                        "layout": "stack",
                        "cam_box": {"x": 0.70, "y": 0.65, "w": 0.28, "h": 0.30},
                    }
                },
            )
            assert patched.status_code == 200
            opts = patched.json()["clip_opts"]
            assert opts["cam_auto"] is False
            assert opts["cam_box"] == {"x": 0.7, "y": 0.65, "w": 0.28, "h": 0.3}

            # Invalid cam_box -> 422
            bad = client.patch(
                f"/api/streamers/{sid}",
                json={"clip_opts": {"cam_box": {"x": "bad", "y": 0, "w": 0.2, "h": 0.2}}},
            )
            assert bad.status_code == 422

            # Deleting streamer removes preview file
            preview_file = streamer_preview_path(settings, sid)
            assert preview_file.is_file()
            assert client.delete(f"/api/streamers/{sid}").status_code == 200
            assert not preview_file.exists()


def test_processing_queue_passes_custom_cam_box_to_neon_client() -> None:
  with tempfile.TemporaryDirectory(prefix="clip_cam_q_") as tmp_dir:
    tmp_path = Path(tmp_dir)
    root = tmp_path / "Clips_Store"
    settings = Settings(
        clips_store_root=str(root),
        raw_recordings_dir=str(root / "raw recordings"),
        drafts_dir=str(root / "drafts"),
        neonclipping_data_dir=str(root / "neon_tmp"),
        db_path=str(root / "db" / "pipeline.sqlite3"),
        enable_background_workers=False,
    )
    ensure_storage_dirs(settings)
    asyncio.run(init_db(settings.db_path))

    chunk_mp4 = _make_synthetic_mp4(Path(settings.raw_recordings_dir) / "chunk.mp4")
    now = utcnow_iso()
    custom_box = {"x": 0.68, "y": 0.62, "w": 0.30, "h": 0.32}

    conn = sqlite3.connect(settings.db_path)
    try:
        sid = int(
            conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES ('CamTest', 'camtest', 'twitch', 'https://twitch.tv/camtest', 'camtest', 1, ?, 0, ?, ?)
                """,
                (
                    '{"cam_auto": false, "layout": "stack", "cam_box": {"x": 0.68, "y": 0.62, "w": 0.3, "h": 0.32}}',
                    now,
                    now,
                ),
            ).lastrowid
        )
        sess_id = int(
            conn.execute(
                "INSERT INTO stream_sessions (streamer_id, title, started_at, status, created_at) VALUES (?, 'Live', ?, 'completed', ?)",
                (sid, now, now),
            ).lastrowid
        )
        chunk_id = int(
            conn.execute(
                "INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status, started_at, created_at) VALUES (?, ?, 1, ?, 'queued', ?, ?)",
                (sess_id, sid, str(chunk_mp4), now, now),
            ).lastrowid
        )
        conn.commit()
    finally:
        conn.close()

    # Verify NeonClippingClient builds --no-cam-auto --cam-json
    fake_engine = tmp_path / "engine"
    (fake_engine / ".venv" / "Scripts").mkdir(parents=True)
    (fake_engine / "pipeline_runner.py").write_text("# stub", encoding="utf-8")
    (fake_engine / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    nc = NeonClippingClient(data_dir=settings.neonclipping_data_dir, engine_dir=fake_engine)
    cmd, _ = nc._build_turbo_invocation(chunk_mp4, cam_auto=False, cam=custom_box)
    assert "--no-cam-auto" in cmd
    assert "--cam-json" in cmd
    assert '"x": 0.68' in cmd[cmd.index("--cam-json") + 1]

    # Verify ProcessingQueueWorker passes cam_auto=False & cam=custom_box
    captured: dict = {}

    class StubNeon:
        async def arefine_and_export_with_turbo(self, chunk_path, output_dir=None, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "clips": []}

    class StubCopy:
        async def generate_youtube_copy(self, **kwargs):
            return {"title": "T", "description": "D", "tags": []}

    class StubDrive:
        async def upload_clip(self, *args, **kwargs):
            return None

    worker = ProcessingQueueWorker(
        settings, neon_client=StubNeon(), copywriter=StubCopy(), google_drive=StubDrive()
    )
    asyncio.run(worker.process_chunk(chunk_id))
    assert captured["cam_auto"] is False
    assert captured["cam"] == custom_box

