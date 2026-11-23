"""Isolated live UX & Studio API runtime stress verification script.

Spins up temporary sandboxed Backend (uvicorn) and Frontend (next start) servers on
ephemeral localhost ports backed ONLY by a temporary directory (never live Clips_Store),
verifies all UI routes (/ , /streamers, /clips, /studio, /accounts, /system) return HTTP 200
without React/Next.js error markers, and executes a live HTTP Studio API stress flow:
  chunked upload -> project create -> preview JPEG -> analyze -> 30 candidate drags ->
  Range 206 stream -> export -> delete -> zero 5xx errors & zero orphan temp files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
BACKEND_PYTHON = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"

ROUTES = ("/", "/streamers", "/clips", "/studio", "/accounts", "/system")

_SANDBOX_SERVER_CODE = r'''
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.environ["SANDBOX_BACKEND_DIR"])
from app.config import get_settings
from app.db import init_db
from app.main import create_app
from app.services.copywriter import CopywriterService
from app.services.google_drive import GoogleDriveService
from app.services.neon_client import NeonClippingClient
import asyncio
import uvicorn

get_settings.cache_clear()
settings = get_settings()
asyncio.run(init_db(settings.db_path))

async def fake_ascout_vod(self, source_path, **_kw):
    return {
        "ok": True,
        "mode": "scout",
        "duration": 12.0,
        "facecam": {"found": False},
        "timeline_curve": {
            "duration": 12.0,
            "step_sec": 1.0,
            "points": [
                {"t": float(i), "hype": 0.2 + 0.1 * (i % 4), "audio": 0.3, "chat": 0.2, "laugh": 0.1}
                for i in range(12)
            ],
        },
        "candidates": [
            {
                "id": "c_01",
                "start": 1.5,
                "end": 8.5,
                "peak_sec": 5.5,
                "duration": 7.0,
                "score": 86,
                "llm": 8.1,
                "visual_action": 8.0,
                "visual_summary": "Sandbox UX verification highlight.",
                "category": "clutch",
                "title": "UX Stress Highlight",
                "hook": "WAIT FOR THIS UX CHECK?!",
                "transcript": "Watch this play right here!",
                "reasons": [{"code": "loudness_spike", "value": 0.8, "t": 5.5}],
                "selected": True,
                "orig_start": 1.5,
                "orig_end": 8.5,
            }
        ],
    }

async def fake_aexport_vod_clips(self, source_path, output_dir, clips_json_path, **_kw):
    clips_spec = json.loads(Path(clips_json_path).read_text(encoding="utf-8"))
    out = []
    for idx, c in enumerate(clips_spec, start=1):
        mp4 = Path(output_dir) / f"ux_exp_{idx}.mp4"
        srt = Path(output_dir) / f"ux_exp_{idx}.srt"
        shutil.copy2(source_path, mp4)
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nUX\n", encoding="utf-8")
        out.append({
            "index": idx,
            "start": c["start"],
            "end": c["end"],
            "duration": round(c["end"] - c["start"], 3),
            "title": c.get("title", "UX Clip"),
            "hook": c.get("hook", ""),
            "transcript": "Watch this play right here!",
            "visual_summary": c.get("visual_summary", ""),
            "score": c.get("score", 86),
            "features": c.get("features", {}),
            "output_mp4": str(mp4),
            "output_srt": str(srt),
        })
    return {"ok": True, "mode": "export", "clips": out}

async def fake_copy(self, **_kw):
    return {"title": "UX Verified Short", "description": "Sandbox copy.", "tags": ["Shorts"], "source": "llm"}

async def fake_drive_upload(self, *_args, **_kw):
    return None

NeonClippingClient.ascout_vod = fake_ascout_vod
NeonClippingClient.aexport_vod_clips = fake_aexport_vod_clips
CopywriterService.generate_youtube_copy = fake_copy
GoogleDriveService.upload_clip = fake_drive_upload

app = create_app()
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["SANDBOX_BE_PORT"]), log_level="warning")
'''


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_req(
    url: str,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    raw_bytes: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = dict(headers or {})
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif raw_bytes is not None:
        data = raw_bytes
        hdrs.setdefault("Content-Type", "application/octet-stream")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def _wait_http_ok(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            code, _, _ = _http_req(url, timeout=3.0)
            if code == 200:
                return
        except Exception as exc:
            last_err = exc
        time.sleep(0.3)
    raise TimeoutError(f"Timed out waiting for {url}: {last_err}")


def _make_synthetic_mp4(dest: Path) -> None:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg binary not found on PATH")
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=12",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _terminate_proc(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    if not BACKEND_PYTHON.is_file():
        raise FileNotFoundError(f"Backend python not found: {BACKEND_PYTHON}")

    be_proc: subprocess.Popen[str] | None = None
    fe_proc: subprocess.Popen[str] | None = None

    with tempfile.TemporaryDirectory(prefix="clip_ux_verify_") as tmp:
        tmp_path = Path(tmp)
        store_root = tmp_path / "Clips_Store"
        raw_dir = store_root / "raw recordings"
        drafts_dir = store_root / "drafts"
        neon_dir = store_root / "neon_tmp"
        db_file = store_root / "db" / "pipeline.sqlite3"
        for d in (raw_dir, drafts_dir, neon_dir, db_file.parent):
            d.mkdir(parents=True, exist_ok=True)

        synth_mp4 = tmp_path / "ux_vod.mp4"
        _make_synthetic_mp4(synth_mp4)
        mp4_bytes = synth_mp4.read_bytes()

        be_port = _free_port()
        fe_port = _free_port()
        be_url = f"http://127.0.0.1:{be_port}"
        fe_url = f"http://127.0.0.1:{fe_port}"

        be_env = os.environ.copy()
        be_env.update(
            {
                "ENABLE_BACKGROUND_WORKERS": "false",
                "CLIPS_STORE_ROOT": str(store_root),
                "RAW_RECORDINGS_DIR": str(raw_dir),
                "DRAFTS_DIR": str(drafts_dir),
                "NEONCLIPPING_DATA_DIR": str(neon_dir),
                "DB_PATH": str(db_file),
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_ALLOWED_CHAT_IDS": "",
                "SANDBOX_BACKEND_DIR": str(BACKEND_DIR),
                "SANDBOX_BE_PORT": str(be_port),
            }
        )

        try:
            print(f"[ux] Starting isolated sandbox backend on {be_url} ...", flush=True)
            be_proc = subprocess.Popen(
                [str(BACKEND_PYTHON), "-c", _SANDBOX_SERVER_CODE],
                cwd=str(BACKEND_DIR),
                env=be_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_http_ok(f"{be_url}/api/health", timeout=30.0)
            print("[ux] Sandbox backend /api/health -> 200 OK", flush=True)

            # Start production Next.js server against built frontend
            npm_cmd = "npm.cmd" if sys.platform == "win32" else "npm"
            fe_env = os.environ.copy()
            fe_env["NEXT_PUBLIC_API_BASE"] = be_url
            fe_env["PORT"] = str(fe_port)
            print(f"[ux] Starting Next.js server on {fe_url} ...", flush=True)
            fe_proc = subprocess.Popen(
                [npm_cmd, "run", "start", "--", "-p", str(fe_port), "-H", "127.0.0.1"],
                cwd=str(FRONTEND_DIR),
                env=fe_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            _wait_http_ok(f"{fe_url}/", timeout=35.0)

            # 1. Verify all 6 frontend routes return HTTP 200 with zero uncaught error boundaries
            for route in ROUTES:
                code, _, body_bytes = _http_req(f"{fe_url}{route}", timeout=15.0)
                html = body_bytes.decode("utf-8", errors="replace")
                assert code == 200, f"Route {route} returned HTTP {code}"
                assert "__next_error__" not in html, f"Route {route} rendered __next_error__"
                assert "Application error: a client-side exception" not in html
                print(f"[ux] GET {fe_url}{route} -> 200 OK ({len(html)} bytes)", flush=True)

            # 2. Live Studio API stress flow against sandbox backend
            _, _, init_raw = _http_req(
                f"{be_url}/api/studio/uploads/init",
                method="POST",
                json_body={"filename": "ux_vod.mp4", "total_size": len(mp4_bytes), "kind": "video"},
            )
            upload_id = json.loads(init_raw)["upload_id"]

            n_chunks = 6
            step = (len(mp4_bytes) + n_chunks - 1) // n_chunks
            for i in range(n_chunks):
                part = mp4_bytes[i * step : (i + 1) * step]
                c_code, _, _ = _http_req(
                    f"{be_url}/api/studio/uploads/{upload_id}/chunk?index={i}",
                    method="PUT",
                    raw_bytes=part,
                )
                assert c_code == 200

            comp_code, _, comp_raw = _http_req(
                f"{be_url}/api/studio/uploads/{upload_id}/complete",
                method="POST",
                json_body={},
            )
            assert comp_code == 200
            uploaded_path = Path(json.loads(comp_raw)["file_path"])
            assert uploaded_path.is_file()

            proj_code, _, proj_raw = _http_req(
                f"{be_url}/api/studio/projects",
                method="POST",
                json_body={"title": "Live UX Stress VOD", "source_type": "upload", "upload_id": upload_id},
            )
            assert proj_code == 200
            pid = json.loads(proj_raw)["id"]

            # Preview JPEG + Range 206 stream
            prev_code, _, prev_bytes = _http_req(f"{be_url}/api/studio/projects/{pid}/preview")
            assert prev_code == 200 and prev_bytes[:3] == b"\xff\xd8\xff"

            rng_code, _, rng_bytes = _http_req(
                f"{be_url}/api/studio/projects/{pid}/video",
                headers={"Range": "bytes=0-1023"},
            )
            assert rng_code == 206 and len(rng_bytes) == 1024

            # Analyze -> poll until analyzed
            ana_code, _, _ = _http_req(
                f"{be_url}/api/studio/projects/{pid}/analyze",
                method="POST",
                json_body={"options": {"clip_count": 1}},
            )
            assert ana_code == 200
            deadline = time.monotonic() + 15.0
            proj_data = {}
            while time.monotonic() < deadline:
                _, _, p_raw = _http_req(f"{be_url}/api/studio/projects/{pid}")
                proj_data = json.loads(p_raw)
                if proj_data["status"] in ("analyzed", "error"):
                    break
                time.sleep(0.05)
            assert proj_data["status"] == "analyzed"

            # 30 rapid candidate drags via live HTTP PATCH
            base_cand = proj_data["candidates"][0]
            for i in range(30):
                patched = [{**base_cand, "start": round(1.0 + i * 0.05, 2), "end": round(8.0 + i * 0.05, 2)}]
                p_code, _, _ = _http_req(
                    f"{be_url}/api/studio/projects/{pid}/candidates",
                    method="PATCH",
                    json_body={"candidates": patched},
                )
                assert p_code == 200

            # Export -> poll until exported
            exp_code, _, _ = _http_req(
                f"{be_url}/api/studio/projects/{pid}/export",
                method="POST",
                json_body={},
            )
            assert exp_code == 200
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                _, _, p_raw = _http_req(f"{be_url}/api/studio/projects/{pid}")
                proj_data = json.loads(p_raw)
                if proj_data["status"] in ("exported", "error"):
                    break
                time.sleep(0.05)
            assert proj_data["status"] == "exported"
            assert len(proj_data["options"].get("last_exported_clip_ids") or []) == 1

            # Delete project -> verify upload & temp chunks removed, exported draft intact
            del_code, _, _ = _http_req(f"{be_url}/api/studio/projects/{pid}", method="DELETE")
            assert del_code == 200
            assert not uploaded_path.exists()
            assert list((store_root / "studio" / "_uploads").iterdir()) == []
            assert len(list(drafts_dir.glob("*.mp4"))) == 1

            print("[ux] ALL LIVE UX ROUTES (6/6) & STUDIO API STRESS FLOW PASSED", flush=True)
            return 0
        finally:
            _terminate_proc(fe_proc)
            _terminate_proc(be_proc)


if __name__ == "__main__":
    raise SystemExit(main())

