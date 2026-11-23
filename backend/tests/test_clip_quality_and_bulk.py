from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from fastapi.testclient import TestClient
import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
ENGINE_DIR = REPO_ROOT / "engines" / "neonclipping"
ENGINE_PYTHON = ENGINE_DIR / ".venv" / "Scripts" / "python.exe"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, utcnow_iso
from app.main import create_app
from app.security import encrypt_token, generate_fernet_key
from app.services.copywriter import CopywriterService
from app.services.recorder import finalize_stream_end
from app.services.youtube_publisher import YouTubePublisherError
from app.storage import draft_clip_filename


def _run_engine_script(script: str, *, data_dir: Path | None = None) -> dict:
    if not ENGINE_PYTHON.is_file():
        pytest.skip(f"Engine Python venv not found at {ENGINE_PYTHON}")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ENGINE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NEONCLIPPING_NO_AUTOUPDATE"] = "1"
    if data_dir is not None:
        env["NEONCLIPPING_DATA_DIR"] = str(data_dir)
    proc = subprocess.run(
        [str(ENGINE_PYTHON), "-c", script],
        cwd=str(ENGINE_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Engine script failed (exit={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


@pytest.fixture()
def isolated_env():
    with tempfile.TemporaryDirectory(prefix="clip_quality_bulk_") as tmp:
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
            chunk_duration_seconds=7200,
            min_standalone_chunk_seconds=3600,
            vlm_min_score=5.0,
            enable_background_workers=False,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, tmp_path


def test_highlights_rank_onset_preroll_and_bucketed_spread() -> None:
    script = """
import json
import numpy as np
import highlights

# Case A: ramp/action from 142..150 culminating in scream peak at 150s
def fake_motion_a(source, duration, report):
    n = int(duration)
    flow = np.ones(n, dtype=float) * 0.2
    cuts = np.zeros(n, dtype=float)
    flow[142:151] = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0]
    return flow, cuts

def fake_audio_a(source):
    n = 300
    rms = np.ones(n, dtype=float) * 0.05
    hf = np.ones(n, dtype=float) * 0.1
    rms[145:152] = [0.25, 0.35, 0.45, 0.55, 0.70, 0.95, 0.20]
    hf[145:152] = 0.6
    return rms, hf

highlights._motion = fake_motion_a
highlights._audio = fake_audio_a
clips_a = highlights.rank("dummy.mp4", 300.0, count=1, length=30.0)

# Case B: 7200s timeline with one huge cluster around 600..900s + valid peaks across timeline
def fake_motion_b(source, duration, report):
    n = int(duration)
    flow = np.ones(n, dtype=float) * 0.1
    cuts = np.zeros(n, dtype=float)
    for t in (620, 680, 740, 800, 860):
        flow[t-2:t+3] = 10.0
    for t in (2400, 4200, 6200):
        flow[t-2:t+3] = 4.0
    return flow, cuts

def fake_audio_b(source):
    n = 7200
    rms = np.ones(n, dtype=float) * 0.02
    hf = np.ones(n, dtype=float) * 0.1
    for t in (620, 680, 740, 800, 860):
        rms[t-2:t+3] = 0.9
        hf[t-2:t+3] = 0.7
    for t in (2400, 4200, 6200):
        rms[t-2:t+3] = 0.45
        hf[t-2:t+3] = 0.5
    return rms, hf

highlights._motion = fake_motion_b
highlights._audio = fake_audio_b
clips_b = highlights.rank("dummy.mp4", 7200.0, count=4, length=30.0)
print(json.dumps({"clips_a": clips_a, "clips_b": clips_b}))
"""
    res = _run_engine_script(script)
    c_a = res["clips_a"][0]
    assert "peak_sec" in c_a
    assert 144.0 <= c_a["peak_sec"] <= 155.0
    assert c_a["start"] <= c_a["peak_sec"] <= c_a["end"]
    assert c_a["start"] <= c_a["peak_sec"] - 10.0

    clips_b = res["clips_b"]
    assert len(clips_b) == 4
    for c in clips_b:
        assert "peak_sec" in c
        assert c["start"] <= c["peak_sec"] <= c["end"]
    starts_b = sorted(c["start"] for c in clips_b)
    assert starts_b[-1] - starts_b[0] >= 3500.0
    # Must not let the 600..900s cluster consume more than 2 slots when distant peaks exist
    assert sum(1 for s in starts_b if 500.0 <= s <= 1000.0) <= 2


def test_smart_snap_preserves_buildup_before_peak_sec() -> None:
    script = """
import json
import smart

c = {"start": 100.0, "end": 160.0, "peak_sec": 140.0}
words = [
    {"start": 38.0, "end": 38.8, "text": "OH"},
    {"start": 38.9, "end": 39.5, "text": "MY!"},
]
smart.snap(c, words, min_len=20.0, max_len=60.0)
print(json.dumps({"clip": c, "words": words}))
"""
    res = _run_engine_script(script)
    c = res["clip"]
    words = res["words"]
    # min_preroll = min(10.0, max(4.0, 60.0 * 0.35)) = 10.0 -> start capped at 140 - 10 = 130.0
    assert c["start"] == pytest.approx(130.0)
    assert c["start"] <= 140.0 <= c["end"]
    assert words[0]["start"] == pytest.approx(8.0)


def test_curation_select_spreads_and_relaxes_gracefully() -> None:
    script = """
import json
import curation

clustered_and_spread = [
    {"start": 300.0, "end": 330.0, "rank": 0.96, "kind": "action", "transcript": "one"},
    {"start": 350.0, "end": 380.0, "rank": 0.95, "kind": "reaction", "transcript": "two"},
    {"start": 400.0, "end": 430.0, "rank": 0.94, "kind": "humour", "transcript": "three"},
    {"start": 2100.0, "end": 2130.0, "rank": 0.88, "kind": "story", "transcript": "four"},
    {"start": 4300.0, "end": 4330.0, "rank": 0.86, "kind": "opinion", "transcript": "five"},
    {"start": 6500.0, "end": 6530.0, "rank": 0.85, "kind": "insight", "transcript": "six"},
]
chosen_spread = curation.select(clustered_and_spread, 3, "balanced", duration=7200.0)

only_clustered = [
    {"start": 100.0, "end": 130.0, "rank": 0.90, "kind": "action", "transcript": "alpha"},
    {"start": 145.0, "end": 175.0, "rank": 0.85, "kind": "reaction", "transcript": "beta"},
    {"start": 190.0, "end": 220.0, "rank": 0.80, "kind": "humour", "transcript": "gamma"},
]
chosen_relaxed = curation.select(only_clustered, 3, "balanced", duration=7200.0)
chosen_compat = curation.select(only_clustered, 2, "balanced")
print(json.dumps({
    "spread": chosen_spread,
    "relaxed": chosen_relaxed,
    "compat": chosen_compat,
}))
"""
    res = _run_engine_script(script)
    spread_starts = sorted(c["start"] for c in res["spread"])
    assert len(spread_starts) == 3
    assert spread_starts[-1] - spread_starts[0] >= 3500.0
    assert len(res["relaxed"]) == 3
    assert len(res["compat"]) == 2


def test_llm_multimodal_400_fallback_and_smart_empty_scream_clamp() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_vlm_unit_") as tmp:
        data_dir = Path(tmp)
        script = """
import io
import json
import urllib.error
import urllib.request
import llm
import smart

llm.save(True, "https://api.example.com/v1", "test-vlm-model", "secret-key-123")

seen_bodies = []
attempt = {"n": 0}

class FakeResponse:
    def __init__(self, data):
        self._data = json.dumps(data).encode("utf-8")
    def read(self, _n=-1):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False

def fake_urlopen(req, timeout=60):
    attempt["n"] += 1
    body = json.loads(req.data.decode("utf-8"))
    seen_bodies.append(body)
    if attempt["n"] == 1:
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", hdrs=None,
            fp=io.BytesIO(b'{"error": "thinking is not supported"}')
        )
    return FakeResponse({
        "choices": [{
            "message": {
                "content": json.dumps({
                    "score": 9,
                    "visual_action": 2,
                    "empty_scream": True,
                    "visual_summary": "Player yells while staring at a static inventory menu.",
                    "kind": "reaction",
                    "title": "Screaming at menu",
                    "hook": "WHY IS HE YELLING AT THIS MENU?!",
                    "evidence": "What is happening right now",
                })
            }
        }]
    })

urllib.request.urlopen = fake_urlopen
judged = smart._judge("cloud:test-vlm-model", "What is happening right now", images=[b"FAKEJPEG"])
print(json.dumps({"seen_bodies": seen_bodies, "judged": judged}))
"""
        res = _run_engine_script(script, data_dir=data_dir)
        bodies = res["seen_bodies"]
        assert len(bodies) == 2
        first_body, second_body = bodies[0], bodies[1]
        assert first_body["thinking"] == {"type": "disabled"}
        assert isinstance(first_body["messages"][0]["content"], list)
        img_parts = [p for p in first_body["messages"][0]["content"] if p.get("type") == "image_url"]
        assert len(img_parts) == 1
        assert img_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert img_parts[0]["image_url"]["detail"] == "high"
        assert "thinking" not in second_body
        assert second_body["response_format"] == {"type": "json_object"}

        judged = res["judged"]
        assert judged["empty_scream"] is True
        assert judged["score"] <= 3.0
        assert judged["visual_action"] == pytest.approx(2.0)
        assert "inventory menu" in judged["visual_summary"]


def test_pipeline_runner_quality_floor_drops_empty_scream_and_handles_zero_clips() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_runner_floor_") as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "neon_data"
        data_dir.mkdir(parents=True, exist_ok=True)
        src_mp4 = tmp_path / "chunk.mp4"
        src_mp4.write_bytes(b"fake-video-bytes")
        out_dir = tmp_path / "out"

        script = f"""
import json
import sys
from pathlib import Path
import action
import app
import captions
import facecam_detect
import highlights
import pipeline_runner
import smart

app.probe = lambda _p: {{"duration": 1800.0}}
highlights.rank = lambda *args, **kwargs: [
    {{"start": 100.0, "end": 135.0, "peak_sec": 120.0, "title": "C1", "score": 90, "reason": "r", "features": {{}}}},
    {{"start": 600.0, "end": 635.0, "peak_sec": 620.0, "title": "C2", "score": 85, "reason": "r", "features": {{}}}},
    {{"start": 1200.0, "end": 1235.0, "peak_sec": 1220.0, "title": "C3", "score": 80, "reason": "r", "features": {{}}}},
]
captions.transcribe = lambda *args, **kwargs: {{
    "words": [{{"start": 1.0, "end": 2.0, "text": "Look"}}, {{"start": 2.1, "end": 3.0, "text": "at"}}, {{"start": 3.1, "end": 4.0, "text": "this"}}, {{"start": 4.1, "end": 5.0, "text": "play!"}}],
    "language": "en",
    "model": "turbo",
}}
captions._turbo_spec = lambda: "large-v3-turbo"
action.analyze = lambda *args, **kwargs: {{"motion": 0.5, "flash": 0.0, "strobe": 0.0, "cuts": 0.1}}
smart._model = lambda *args, **kwargs: "cloud:test-vlm"
smart.extract_storyboard_grid = lambda *args, **kwargs: b"FAKEGRID"
facecam_detect.detect = lambda *args, **kwargs: {{"found": False}}

judge_results_mixed = [
    {{"score": 8.0, "title": "Good Play", "hook": "LOOK AT THIS PLAY!", "kind": "action", "evidence": "Look at this play!", "focus_match": None, "visual_action": 8.0, "empty_scream": False, "visual_summary": "Clutch 1v3 headshot in smoke."}},
    {{"score": 3.0, "title": "Empty Scream", "hook": "LOOK AT THIS PLAY!", "kind": "reaction", "evidence": "Look at this play!", "focus_match": None, "visual_action": 1.0, "empty_scream": True, "visual_summary": "Streamer yells at static pause menu."}},
    {{"score": 3.5, "title": "Weak Moment", "hook": "LOOK AT THIS PLAY!", "kind": "other", "evidence": "Look at this play!", "focus_match": None, "visual_action": 3.0, "empty_scream": False, "visual_summary": "Walking silently down hallway."}},
]
idx_box = {{"i": 0}}
def fake_judge_mixed(*args, **kwargs):
    res = judge_results_mixed[idx_box["i"]]
    idx_box["i"] += 1
    return res

export_calls = []
def fake_export(body):
    jid = f"job{{len(export_calls) + 1}}"
    export_calls.append(jid)
    (Path(r"{data_dir}") / f"{{jid}}.mp4").write_bytes(b"exported-mp4")
    app.state[jid] = {{"status": "done", "encoder": "h264_nvenc", "caption_words": 4, "meta": {{}}}}
    return {{"job": jid}}

app.export = fake_export
smart._judge = fake_judge_mixed

res_mixed = pipeline_runner.run_chunk_pipeline(
    r"{src_mp4}",
    output_dir=r"{out_dir}",
    data_dir=r"{data_dir}",
    count=3,
    min_score=5.0,
)

# Now all candidates fail floor -> clips: [] cleanly without calling app.export
export_calls.clear()
def fake_judge_all_bad(*args, **kwargs):
    return {{"score": 3.0, "title": "Bad", "hook": "LOOK AT THIS PLAY!", "kind": "other", "evidence": "Look at this play!", "focus_match": None, "visual_action": 1.0, "empty_scream": True, "visual_summary": "Static lobby screen."}}
smart._judge = fake_judge_all_bad
res_empty = pipeline_runner.run_chunk_pipeline(
    r"{src_mp4}",
    output_dir=r"{out_dir}",
    data_dir=r"{data_dir}",
    count=3,
    min_score=5.0,
)
print(json.dumps({{"mixed": res_mixed, "empty": res_empty, "empty_exports": len(export_calls)}}))
"""
        res = _run_engine_script(script, data_dir=data_dir)
        mixed = res["mixed"]
        assert mixed["filtered_out_by_quality_floor"] == 2
        assert len(mixed["clips"]) == 1
        assert mixed["clips"][0]["title"] == "Good Play"
        assert mixed["clips"][0]["visual_summary"] == "Clutch 1v3 headshot in smoke."
        assert mixed["clips"][0]["empty_scream"] is False

        empty = res["empty"]
        assert empty["ok"] is True
        assert empty["clips"] == []
        assert empty["filtered_out_by_quality_floor"] == 3
        assert res["empty_exports"] == 0


def test_recorder_standalone_1h15m_queued_and_30m_discarded(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, tmp_path = isolated_env
    now = utcnow_iso()
    conn = sqlite3.connect(settings.db_path)
    try:
        streamer_id = conn.execute(
            """
            INSERT INTO streamers (
                name, slug, platform, channel_url, channel_ref,
                auto_watch, clip_opts_json, is_live, created_at, updated_at
            )
            VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', 1, '{}', 1, ?, ?)
            """,
            (now, now),
        ).lastrowid
        session_1 = conn.execute(
            "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'recording', ?)",
            (streamer_id, now, now),
        ).lastrowid
        session_2 = conn.execute(
            "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'recording', ?)",
            (streamer_id, now, now),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    from app.services import recorder as recorder_mod

    durations_by_name = {"tail_75m.mkv": 4500.0, "tail_30m.mkv": 1800.0}

    async def fake_readable_duration(path: str | Path) -> tuple[float, Path]:
        p = Path(path)
        return durations_by_name[p.name], p

    async def fake_remux_to_mp4(src_mkv: str | Path, dst_mp4: str | Path) -> Path:
        dst = Path(dst_mp4)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"remuxed-mp4")
        return dst

    async def fake_probe_duration(_path: str | Path) -> float:
        return 4500.0

    monkeypatch.setattr(recorder_mod, "readable_duration", fake_readable_duration)
    monkeypatch.setattr(recorder_mod, "remux_to_mp4", fake_remux_to_mp4)
    monkeypatch.setattr(recorder_mod, "probe_duration", fake_probe_duration)

    tail_75m = tmp_path / "raw_tmp" / "tail_75m.mkv"
    tail_30m = tmp_path / "raw_tmp" / "tail_30m.mkv"
    tail_75m.parent.mkdir(parents=True, exist_ok=True)
    tail_75m.write_bytes(b"75m-mkv")
    tail_30m.write_bytes(b"30m-mkv")

    queued_ids: list[int] = []

    async def on_queued(cid: int) -> None:
        queued_ids.append(cid)

    start_dt = datetime(2026, 11, 23, 1, 0, 0, tzinfo=timezone.utc)
    res_75m = asyncio.run(
        finalize_stream_end(
            db_path=settings.db_path,
            session_id=int(session_1),
            streamer_id=int(streamer_id),
            streamer_slug="tarik",
            tail_mkv_path=tail_75m,
            chunk_index=1,
            chunk_started_at=start_dt,
            settings=settings,
            on_chunk_queued=on_queued,
        )
    )
    assert res_75m["action"] == "queued_standalone"
    assert res_75m["status"] == "queued"
    assert len(queued_ids) == 1

    res_30m = asyncio.run(
        finalize_stream_end(
            db_path=settings.db_path,
            session_id=int(session_2),
            streamer_id=int(streamer_id),
            streamer_slug="tarik",
            tail_mkv_path=tail_30m,
            chunk_index=1,
            chunk_started_at=start_dt,
            settings=settings,
            on_chunk_queued=on_queued,
        )
    )
    assert res_30m["action"] == "discarded_short"
    assert res_30m["status"] == "discarded_short"
    assert len(queued_ids) == 1


def test_copywriter_sends_thinking_disabled_visual_summary_and_400_fallback(
    isolated_env,
) -> None:
    settings, _ = isolated_env
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_payloads.append(body)
        if len(seen_payloads) == 1:
            return httpx.Response(400, json={"error": "unknown parameter: thinking"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "title": "Insane 360 Clutch on Ascent",
                                    "description": "Tarik lands a crisp 360 headshot to win the round!",
                                    "tags": ["Valorant", "Shorts"],
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
                transcript="NO WAY HE HIT THAT!",
                neon_title="360 Clutch",
                neon_hook="NO WAY HE HIT THAT!",
                streamer_name="Tarik",
                stream_title="Radiant Ranked",
                platform="twitch",
                visual_summary="Player lands a 360 sniper headshot through smoke.",
            )

    res = asyncio.run(_run())
    assert len(seen_payloads) == 2
    first, second = seen_payloads[0], seen_payloads[1]
    assert first["thinking"] == {"type": "disabled"}
    assert first["max_tokens"] == 400
    assert first["response_format"] == {"type": "json_object"}
    assert "Visual Context: Player lands a 360 sniper headshot through smoke." in first["messages"][1]["content"]
    assert "thinking" not in second
    assert second["response_format"] == {"type": "json_object"}
    assert res["source"] == "llm"
    assert res["title"] == "Insane 360 Clutch on Ascent"


def test_bulk_clips_approve_reject_delete_endpoints(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, _ = isolated_env
    now = utcnow_iso()
    drafts_dir = Path(settings.drafts_dir)

    conn = sqlite3.connect(settings.db_path)
    try:
        streamer_id = int(
            conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', 1, '{}', 1, ?, ?)
                """,
                (now, now),
            ).lastrowid
        )
        session_id = int(
            conn.execute(
                "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'completed', ?)",
                (streamer_id, now, now),
            ).lastrowid
        )
        chunk_id = int(
            conn.execute(
                "INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status, created_at) VALUES (?, ?, 1, 'raw.mp4', 'processed', ?)",
                (session_id, streamer_id, now),
            ).lastrowid
        )
        acc_id = int(
            conn.execute(
                """
                INSERT INTO youtube_accounts (
                    channel_id, channel_title, email, oauth_provider,
                    encrypted_refresh_token, encrypted_access_token,
                    token_expires_at, scopes, status, created_at, updated_at
                )
                VALUES ('UC_BULK', 'Bulk Channel', 'b@example.com', 'google', ?, ?, ?, 'https://www.googleapis.com/auth/youtube.upload', 'active', ?, ?)
                """,
                (
                    encrypt_token("ref", key=settings.token_encryption_key),
                    encrypt_token("acc", key=settings.token_encryption_key),
                    "2099-01-01T00:00:00+00:00",
                    now,
                    now,
                ),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO streamer_targets (streamer_id, youtube_account_id, default_privacy, default_tags_json, enabled, created_at) VALUES (?, ?, 'public', '[]', 1, ?)",
            (streamer_id, acc_id, now),
        )

        dt = datetime(2026, 11, 23, 2, 0, 0, tzinfo=timezone.utc)
        clip_files: list[tuple[Path, Path, Path]] = []
        clip_ids: list[int] = []
        statuses = ("draft", "draft", "posted")
        for idx, st in enumerate(statuses, start=1):
            mp4 = drafts_dir / draft_clip_filename("tarik", dt, 1, idx, ext="mp4")
            srt = drafts_dir / draft_clip_filename("tarik", dt, 1, idx, ext="srt")
            jpg = drafts_dir / draft_clip_filename("tarik", dt, 1, idx, ext="jpg")
            mp4.write_bytes(f"mp4-{idx}".encode("utf-8"))
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
            jpg.write_bytes(b"jpg-bytes")
            clip_files.append((mp4, srt, jpg))
            cid = int(
                conn.execute(
                    """
                    INSERT INTO clip_drafts (
                        streamer_id, session_id, chunk_id, draft_number,
                        title, description, tags_json, file_path, thumbnail_path,
                        start_sec, end_sec, duration_sec, score, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'One sentence.', '["Shorts"]', ?, ?, 0, 30, 30, 85, ?, ?, ?)
                    """,
                    (streamer_id, session_id, chunk_id, idx, f"Clip {idx}", str(mp4), str(jpg), st, now, now),
                ).lastrowid
            )
            clip_ids.append(cid)
        conn.commit()
    finally:
        conn.close()

    c1, c2, c3 = clip_ids

    async def fake_publish_clip(self, db, clip_id: int, **_kwargs):
        if clip_id == c2:
            raise YouTubePublisherError("YouTube quota exceeded")
        await db.execute(
            "UPDATE clip_drafts SET status = 'posted', updated_at = ? WHERE id = ?",
            (utcnow_iso(), clip_id),
        )
        await db.execute(
            """
            INSERT INTO publications (
                clip_draft_id, youtube_account_id,
                youtube_video_id, youtube_url, privacy_status, status, published_at, created_at, updated_at
            )
            VALUES (?, ?, 'vid123', 'https://youtube.com/shorts/vid123', 'unlisted', 'posted', ?, ?, ?)
            """,
            (clip_id, acc_id, now, now, now),
        )
        await db.commit()
        return []

    from app.services.youtube_publisher import YouTubePublisherService

    monkeypatch.setattr(YouTubePublisherService, "publish_clip", fake_publish_clip)

    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        # 1. Bulk approve with partial failure + missing ID
        res_approve = client.post(
            "/api/clips/bulk/approve",
            json={"clip_ids": [c1, c2, 9999], "privacy_status": "unlisted", "publish_now": True},
        )
        assert res_approve.status_code == 200
        body_approve = res_approve.json()
        assert len(body_approve["succeeded"]) == 1
        assert body_approve["succeeded"][0]["clip"]["id"] == c1
        assert body_approve["succeeded"][0]["clip"]["status"] == "posted"
        assert len(body_approve["succeeded"][0]["publications"]) == 1
        assert body_approve["failed"] == [
            {"clip_id": c2, "detail": "YouTube quota exceeded"},
            {"clip_id": 9999, "detail": "Clip not found"},
        ]

        # 2. Bulk reject skips posted clips (c1 & c3), updates c2 to kept_local, reports missing 9999
        res_reject = client.post(
            "/api/clips/bulk/reject",
            json={"clip_ids": [c1, c2, c3, 9999]},
        )
        assert res_reject.status_code == 200
        body_reject = res_reject.json()
        assert [item["id"] for item in body_reject["updated"]] == [c2]
        assert body_reject["updated"][0]["status"] == "kept_local"
        assert body_reject["skipped"] == [c1, c3]
        assert body_reject["missing"] == [9999]

        # 3. Bulk delete removes local files (.mp4/.srt/.jpg) and DB rows
        res_delete = client.post(
            "/api/clips/bulk/delete",
            json={"clip_ids": [c1, c3, 9999], "delete_drive": False},
        )
        assert res_delete.status_code == 200
        body_delete = res_delete.json()
        assert body_delete["deleted_ids"] == [c1, c3]
        assert body_delete["failed"] == [{"clip_id": 9999, "detail": "Clip not found"}]

    for idx in (0, 2):
        for artifact in clip_files[idx]:
            assert not artifact.exists(), f"Expected deleted artifact to be gone: {artifact}"
    for artifact in clip_files[1]:
        assert artifact.exists(), f"Expected non-deleted artifact to remain: {artifact}"

    conn_check = sqlite3.connect(settings.db_path)
    try:
        remaining = [
            row[0] for row in conn_check.execute("SELECT id FROM clip_drafts ORDER BY id").fetchall()
        ]
        assert remaining == [c2]
    finally:
        conn_check.close()


def test_drive_delete_retry_backoff_and_bulk_delete_pacing(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import open_connection
    from app.routers import clips as clips_router
    from app.services.google_drive import GoogleDriveService

    settings, _ = isolated_env
    now = utcnow_iso()
    drafts_dir = Path(settings.drafts_dir)

    conn = sqlite3.connect(settings.db_path)
    try:
        streamer_id = int(
            conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', 1, '{}', 1, ?, ?)
                """,
                (now, now),
            ).lastrowid
        )
        session_id = int(
            conn.execute(
                "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'completed', ?)",
                (streamer_id, now, now),
            ).lastrowid
        )
        chunk_id = int(
            conn.execute(
                "INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status, created_at) VALUES (?, ?, 1, 'raw.mp4', 'processed', ?)",
                (session_id, streamer_id, now),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO youtube_accounts (
                channel_id, channel_title, email, oauth_provider,
                encrypted_refresh_token, encrypted_access_token,
                token_expires_at, scopes, status, created_at, updated_at
            )
            VALUES ('drive-only:test@example.com', 'Drive', 'test@example.com', 'google_drive', ?, ?, ?, 'https://www.googleapis.com/auth/drive.file', 'active', ?, ?)
            """,
            (
                encrypt_token("ref", key=settings.token_encryption_key),
                encrypt_token("acc", key=settings.token_encryption_key),
                "2099-01-01T00:00:00+00:00",
                now,
                now,
            ),
        )
        dt = datetime(2026, 11, 23, 3, 0, 0, tzinfo=timezone.utc)
        clip_ids: list[int] = []
        for idx in (1, 2, 3):
            mp4 = drafts_dir / draft_clip_filename("tarik", dt, 1, idx, ext="mp4")
            mp4.write_bytes(b"mp4")
            cid = int(
                conn.execute(
                    """
                    INSERT INTO clip_drafts (
                        streamer_id, session_id, chunk_id, draft_number,
                        title, description, tags_json, file_path,
                        drive_file_id, drive_url, start_sec, end_sec, duration_sec,
                        score, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'One.', '[]', ?, ?, ?, 0, 30, 30, 80, 'draft', ?, ?)
                    """,
                    (
                        streamer_id,
                        session_id,
                        chunk_id,
                        idx,
                        f"Drive Clip {idx}",
                        str(mp4),
                        f"drv_{idx}",
                        f"https://drive.google.com/file/d/drv_{idx}/view",
                        now,
                        now,
                    ),
                ).lastrowid
            )
            clip_ids.append(cid)
        conn.commit()
    finally:
        conn.close()

    # 1. GoogleDriveService.delete_file retries on 429, 403 userRateLimitExceeded, and 503
    attempts = {"n": 0}

    def retry_handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
        if attempts["n"] == 2:
            return httpx.Response(
                403,
                json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
            )
        if attempts["n"] == 3:
            return httpx.Response(503, text="backend unavailable")
        return httpx.Response(204)

    async def _test_retry() -> bool:
        db = await open_connection(settings.db_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(retry_handler)) as client:
                svc = GoogleDriveService(settings)
                return await svc.delete_file(
                    db,
                    "drv_retry",
                    http_client=client,
                    max_retries=4,
                    base_retry_delay=0.0,
                )
        finally:
            await db.close()

    assert asyncio.run(_test_retry()) is True
    assert attempts["n"] == 4

    # 2. bulk_delete_clips paces Drive deletes, shares one client, and surfaces drive_failed_ids
    sleeps: list[float] = []
    seen_clients: list[int] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def fake_delete_file(self, db, file_id: str, account_id=None, *, http_client=None, **_kw) -> bool:
        assert http_client is not None
        seen_clients.append(id(http_client))
        return file_id != "drv_2"

    monkeypatch.setattr(clips_router.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(GoogleDriveService, "delete_file", fake_delete_file)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        res = client.post(
            "/api/clips/bulk/delete",
            json={"clip_ids": clip_ids, "delete_drive": True},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["deleted_ids"] == clip_ids
        assert body["failed"] == []
        assert body["drive_failed_ids"] == [clip_ids[1]]

    # 3 Drive deletes -> 2 pacing intervals between them, all on 1 shared AsyncClient
    assert len(sleeps) == 2
    assert all(s == pytest.approx(clips_router.BULK_DRIVE_DELETE_INTERVAL_SEC) for s in sleeps)
    assert len(set(seen_clients)) == 1


def test_schedule_clip_cancel_bulk_stagger_and_scheduler_worker(
    isolated_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from app.services.publish_scheduler import PublishSchedulerWorker
    from app.services.youtube_publisher import YouTubePublisherService

    settings, _ = isolated_env
    now = utcnow_iso()
    drafts_dir = Path(settings.drafts_dir)

    conn = sqlite3.connect(settings.db_path)
    try:
        streamer_id = int(
            conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', 1, '{}', 1, ?, ?)
                """,
                (now, now),
            ).lastrowid
        )
        session_id = int(
            conn.execute(
                "INSERT INTO stream_sessions (streamer_id, started_at, status, created_at) VALUES (?, ?, 'completed', ?)",
                (streamer_id, now, now),
            ).lastrowid
        )
        chunk_id = int(
            conn.execute(
                "INSERT INTO recording_chunks (session_id, streamer_id, chunk_index, file_path, status, created_at) VALUES (?, ?, 1, 'raw.mp4', 'processed', ?)",
                (session_id, streamer_id, now),
            ).lastrowid
        )
        dt = datetime(2026, 11, 23, 5, 0, 0, tzinfo=timezone.utc)
        clip_ids: list[int] = []
        for idx in (1, 2, 3):
            mp4 = drafts_dir / draft_clip_filename("tarik", dt, 1, idx, ext="mp4")
            mp4.write_bytes(b"mp4")
            cid = int(
                conn.execute(
                    """
                    INSERT INTO clip_drafts (
                        streamer_id, session_id, chunk_id, draft_number,
                        title, description, tags_json, file_path,
                        start_sec, end_sec, duration_sec, score, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'One.', '[]', ?, 0, 30, 30, 80, 'draft', ?, ?)
                    """,
                    (streamer_id, session_id, chunk_id, idx, f"Sched Clip {idx}", str(mp4), now, now),
                ).lastrowid
            )
            clip_ids.append(cid)
        conn.commit()
    finally:
        conn.close()

    c1, c2, c3 = clip_ids
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    future_1 = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0)
    past_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    with TestClient(app) as client:
        # Reject past schedule
        bad = client.post(
            f"/api/clips/{c1}/schedule",
            json={"scheduled_at": past_ts, "privacy_status": "public"},
        )
        assert bad.status_code == 400

        # Schedule single clip + cancel it
        r1 = client.post(
            f"/api/clips/{c1}/schedule",
            json={"scheduled_at": future_1.isoformat(), "privacy_status": "unlisted"},
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "scheduled"
        assert r1.json()["scheduled_privacy_status"] == "unlisted"

        rc = client.delete(f"/api/clips/{c1}/schedule")
        assert rc.status_code == 200
        assert rc.json()["status"] == "draft"
        assert rc.json()["scheduled_publish_at"] is None

        # Bulk schedule c1 & c2 staggered 60 minutes apart
        rb = client.post(
            "/api/clips/bulk/schedule",
            json={
                "clip_ids": [c1, c2],
                "scheduled_at": future_1.isoformat(),
                "interval_minutes": 60,
                "privacy_status": "public",
            },
        )
        assert rb.status_code == 200
        updated = rb.json()["updated"]
        assert [u["id"] for u in updated] == [c1, c2]
        assert updated[0]["scheduled_publish_at"] == future_1.isoformat()
        assert updated[1]["scheduled_publish_at"] == (future_1 + timedelta(minutes=60)).isoformat()

    # Worker publishes ONLY c1 when now is +2h 15m (c2 at +3h remains scheduled)
    published_calls: list[tuple[int, str | None]] = []

    async def fake_publish_clip(self, db, clip_id: int, *, privacy_override=None, **_kw):
        published_calls.append((clip_id, privacy_override))
        await db.execute(
            "UPDATE clip_drafts SET status = 'posted', updated_at = ? WHERE id = ?",
            (utcnow_iso(), clip_id),
        )
        await db.commit()
        return {"clip_id": clip_id, "status": "posted", "publications": [], "succeeded": 1, "failed": 0}

    monkeypatch.setattr(YouTubePublisherService, "publish_clip", fake_publish_clip)
    worker = PublishSchedulerWorker(settings)
    run_res = asyncio.run(worker.run_once(now=future_1 + timedelta(minutes=15)))
    assert run_res["published_ids"] == [c1]
    assert run_res["failed_ids"] == []
    assert published_calls == [(c1, "public")]

    conn_check = sqlite3.connect(settings.db_path)
    try:
        rows = dict(conn_check.execute("SELECT id, status FROM clip_drafts").fetchall())
        assert rows[c1] == "posted"
        assert rows[c2] == "scheduled"
        assert rows[c3] == "draft"
    finally:
        conn_check.close()

