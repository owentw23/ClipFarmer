"""Comprehensive unit, integration, and stress test suite for Highlight Detection v2 & VOD Studio."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from fastapi.testclient import TestClient
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
ENGINE_DIR = REPO_ROOT / "engines" / "neonclipping"
ENGINE_PYTHON = ENGINE_DIR / ".venv" / "Scripts" / "python.exe"
LIVE_CED_MODEL_DIR = Path(r"C:\Users\wirya\Documents\Clips_Store\neon_tmp\models\ced-mini")

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings, get_settings
from app.db import init_db, utcnow_iso
from app.main import create_app
from app.security import generate_fernet_key
from app.services.chat_recorder import LiveChatRecorder, format_twitch_pong, parse_twitch_irc_line
from app.services.retention_gc import RetentionGC


def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    pytest.skip("ffmpeg binary not found on PATH")
    return "ffmpeg"


def _make_synthetic_mp4(dest: Path, duration_sec: float = 12.0) -> Path:
    ffmpeg_bin = _find_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=30:duration={duration_sec}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=44100:duration={duration_sec}",
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
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"ffmpeg failed: {proc.stderr}"
    assert dest.is_file() and dest.stat().st_size > 1000
    return dest


def _run_engine_script(script: str, *, data_dir: Path | None = None, timeout: int = 120) -> dict:
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
        timeout=timeout,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Engine script failed (exit={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout[-1500:]}\nSTDERR:\n{proc.stderr[-1500:]}"
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


@pytest.fixture()
def studio_env():
    with tempfile.TemporaryDirectory(prefix="clip_v2_studio_") as tmp:
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
            llm_model="deepseek-v4-1-flash-economy-261017",
            llm_api_key="test-llm-key",
            enable_background_workers=False,
        )
        asyncio.run(init_db(settings.db_path))
        yield settings, tmp_path


# ---------------------------------------------------------------------------
# 1. Chat Signals + 100k-Message Stress Test
# ---------------------------------------------------------------------------


def test_chat_signals_filters_lag_formats_and_100k_stress() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_v2_chat_") as tmp:
        tmp_path = Path(tmp)
        script = f"""
import json
import time
from pathlib import Path
import numpy as np
from v2 import chat_signals

tmp = Path(r"{tmp_path}")

# 1. Test all 4 supported chat file formats
p_jsonl = tmp / "a.chat.jsonl"
p_jsonl.write_text('\\n'.join([
    json.dumps({{"t": 12.0, "author": "alice", "text": "KEKW", "kind": "message"}}),
    json.dumps({{"t": 5.0, "author": "Nightbot", "text": "follow!", "kind": "message"}}),
]), encoding="utf-8")
m_jsonl = chat_signals.load_chat_messages(p_jsonl)
assert len(m_jsonl) == 2 and m_jsonl[0]["t"] == 5.0

p_td = tmp / "td.json"
p_td.write_text(json.dumps({{"comments": [
    {{"content_offset_seconds": 42.5, "commenter": {{"name": "bob"}}, "message": {{"body": "Pog"}}}},
]}}), encoding="utf-8")
m_td = chat_signals.load_chat_messages(p_td)
assert len(m_td) == 1 and m_td[0]["author"] == "bob" and m_td[0]["text"] == "Pog"

p_cd = tmp / "cd.json"
p_cd.write_text(json.dumps([
    {{"time_in_seconds": 18.2, "author": {{"name": "carol"}}, "message": "LULW", "message_type": "text_message"}},
]), encoding="utf-8")
m_cd = chat_signals.load_chat_messages(p_cd)
assert len(m_cd) == 1 and m_cd[0]["author"] == "carol"

p_yt = tmp / "yt.live_chat.json"
p_yt.write_text(json.dumps({{"replayChatItemAction": {{
    "videoOffsetTimeMsec": "31500",
    "actions": [{{"addChatItemAction": {{"item": {{"liveChatTextMessageRenderer": {{
        "authorName": {{"simpleText": "dave"}},
        "message": {{"runs": [{{"text": "CLIP IT"}}]}},
    }}}}}}}}],
}}}}), encoding="utf-8")
m_yt = chat_signals.load_chat_messages(p_yt)
assert len(m_yt) == 1 and m_yt[0]["t"] == 31.5 and m_yt[0]["text"] == "CLIP IT"

# 2. Filtering, poll burst drop, unique-author 10s bucketing, copy-paste wave, and 6s lag estimation
duration = 600.0
arousal = np.zeros(600, dtype=np.float32)
msgs = []

# Bots, !commands, and paid events should be dropped
msgs.append({{"t": 10.0, "author": "nightbot", "text": "KEKW", "kind": "message"}})
msgs.append({{"t": 11.0, "author": "streamelements", "text": "LUL", "kind": "message"}})
msgs.append({{"t": 12.0, "author": "viewer1", "text": "!discord", "kind": "message"}})
msgs.append({{"t": 13.0, "author": "viewer2", "text": "superchat", "kind": "paid"}})

# Poll burst: 25 unique authors voting "1" / "yes" inside 10s around t=50s -> dropped
for i in range(25):
    msgs.append({{"t": 50.0 + i * 0.3, "author": f"voter_{{i}}", "text": "1" if i % 2 == 0 else "yes", "kind": "message"}})

# Same author spamming 15 times inside one 10s bucket (t=80..88) -> counts once
for i in range(15):
    msgs.append({{"t": 80.0 + i * 0.5, "author": "spammer", "text": "KEKW", "kind": "message"}})

# Copy-paste wave at t=140s: 8 distinct authors send identical phrase
for i in range(8):
    msgs.append({{"t": 140.0 + i * 0.5, "author": f"waver_{{i}}", "text": "NO WAY HE HIT THAT", "kind": "message"}})

# Synthetic spikes with exact +6.0s chat reaction delay behind arousal peaks
spike_times = [200, 300, 400, 500]
for st in spike_times:
    arousal[st - 1 : st + 3] = 0.95
    for k in range(60):
        msgs.append({{
            "t": float(st + 6.0 + (k % 3) * 0.4),
            "author": f"fan_{{st}}_{{k}}",
            "text": "KEKW OMEGALUL" if k % 2 == 0 else "CLIP IT ????",
            "kind": "message",
        }})

# Baseline chatter so filtered count >= 200 and background median is stable
for sec in range(0, 600, 3):
    msgs.append({{"t": float(sec) + 0.1, "author": f"bg_{{sec % 40}}", "text": "nice stream", "kind": "message"}})

res = chat_signals.compute_chat_signals(msgs, duration, arousal=arousal, default_lag_s=5.0)
lag = res["chat_lag_s"]
assert abs(lag - 6.0) <= 1.0, f"Expected lag ~6.0s, got {{lag}}"
# After left-shifting by lag, t=200 must show elevated chat_rate and chat_react; poll at t=50 must stay quiet
assert float(res["chat_rate"][198:203].max()) > 0.5
assert float(res["chat_react"][198:203].max()) > 0.3
assert float(res["chat_rate"][48:55].max()) < 0.25
# Spammer bucket check: t=80..89 should not exceed 2 unique authors
bucket_80 = [m for m in res["filtered_messages"] if m["author"] == "spammer"]
assert len(bucket_80) == 15  # filtered_messages preserves messages, while cnt/react cap per 10s bucket
assert float(res["chat_rate"][80:89].max()) < 0.35

# window_chat_lines checks lag shift, dedup, and [MM:SS] formatting
lines = chat_signals.window_chat_lines(res["filtered_messages"], 195.0, 210.0, lag_s=lag, max_lines=20)
assert len(lines) > 0 and all(line.startswith("[03:") for line in lines)

# 3. 100,000-message performance stress test (< 3.0s)
stress_msgs = [
    {{
        "t": float((i * 0.14) % 14400.0),
        "author": f"user_{{i % 5000}}",
        "text": "KEKW Pog" if i % 7 == 0 else ("!uptime" if i % 19 == 0 else "gg wp"),
        "kind": "message",
    }}
    for i in range(100_000)
]
t0 = time.perf_counter()
stress_res = chat_signals.compute_chat_signals(stress_msgs, 14400.0)
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "lag": lag,
    "stress_elapsed": round(elapsed, 4),
    "stress_count": stress_res["message_count"],
    "curve_len": len(stress_res["chat_rate"]),
}}))
"""
        res = _run_engine_script(script)
        assert abs(res["lag"] - 6.0) <= 1.0
        assert res["stress_elapsed"] < 3.0, f"100k chat took {res['stress_elapsed']}s (>= 3.0s)"
        assert res["stress_count"] > 90_000
        assert res["curve_len"] == 14400


# ---------------------------------------------------------------------------
# 2. Audio Events (CED-mini ONNX) + Missing-Model Fallback
# ---------------------------------------------------------------------------


def test_audio_events_onnx_and_missing_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_v2_audio_") as tmp:
        tmp_path = Path(tmp)
        wav_path = tmp_path / "synth.wav"
        ffmpeg_bin = _find_ffmpeg()
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=16000:duration=4",
                "-ac",
                "1",
                str(wav_path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )

        # Copy cached CED-mini model to temp sandbox if available so we never touch live Clips_Store
        cached_avail = (
            (LIVE_CED_MODEL_DIR / "model.int8.onnx").is_file()
            and (LIVE_CED_MODEL_DIR / "class_labels_indices.csv").is_file()
        )
        ced_sandbox = tmp_path / "ced_with_model"
        if cached_avail:
            target_dir = ced_sandbox / "models" / "ced-mini"
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LIVE_CED_MODEL_DIR / "model.int8.onnx", target_dir / "model.int8.onnx")
            shutil.copy2(LIVE_CED_MODEL_DIR / "class_labels_indices.csv", target_dir / "class_labels_indices.csv")

        empty_sandbox = tmp_path / "ced_empty"
        empty_sandbox.mkdir(parents=True, exist_ok=True)

        script = f"""
import json
import urllib.error
import urllib.request
from pathlib import Path
from v2 import audio_events

wav = Path(r"{wav_path}")
empty_dir = Path(r"{empty_sandbox}")
assert audio_events.is_model_available(empty_dir) is False

# Block network download and verify graceful {{}} fallback
orig_urlopen = urllib.request.urlopen
def fail_urlopen(*args, **kwargs):
    raise urllib.error.URLError("offline test")
urllib.request.urlopen = fail_urlopen
fallback_res = audio_events.detect_audio_events(wav, 4.0, empty_dir)
assert fallback_res == {{}}, f"Expected empty dict fallback, got {{fallback_res}}"
urllib.request.urlopen = orig_urlopen

has_cached = {repr(cached_avail)}
ran_onnx = False
shapes_ok = False
if has_cached:
    ced_dir = Path(r"{ced_sandbox}")
    assert audio_events.is_model_available(ced_dir) is True
    ev = audio_events.detect_audio_events(wav, 4.0, ced_dir)
    ran_onnx = True
    assert set(ev.keys()) == {{"laugh", "scream", "cheer"}}
    for k, arr in ev.items():
        assert len(arr) == 4
        assert float(arr.min()) >= 0.0 and float(arr.max()) <= 1.0
    shapes_ok = True

print(json.dumps({{"ran_onnx": ran_onnx, "shapes_ok": shapes_ok}}))
"""
        res = _run_engine_script(script)
        if cached_avail:
            assert res["ran_onnx"] is True and res["shapes_ok"] is True


# ---------------------------------------------------------------------------
# 3. Two-Way story_window() Boundaries
# ---------------------------------------------------------------------------


def test_story_window_two_way_boundaries_clamping_hints_and_priors() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_v2_bounds_") as tmp:
        priors_path = Path(tmp) / "boundary_priors.json"
        priors_path.write_text(
            json.dumps(
                {
                    "preroll": {"clutch": {"min": 18.0, "target": 24.0, "max": 32.0}},
                    "tail": {"default": {"min": 3.0, "target": 7.0, "max": 10.0}},
                }
            ),
            encoding="utf-8",
        )
        script = f"""
import json
import numpy as np
from v2 import boundaries

dur = 300.0
# Build clean sentence boundaries at 76.0 (pause 1.2s before 77.2), 100.0 (pause 1.5s), 106.0 (pause 1.1s)
words = [
    {{"start": 74.0, "end": 75.8, "text": "Wait"}},
    {{"start": 75.9, "end": 76.8, "text": "watch."}},
    {{"start": 78.2, "end": 82.0, "text": "Setup"}},
    {{"start": 82.2, "end": 95.0, "text": "continues"}},
    {{"start": 95.2, "end": 99.0, "text": "boom."}},
    {{"start": 100.6, "end": 105.0, "text": "WHAT?!"}},
    {{"start": 106.4, "end": 109.0, "text": "Insane."}},
]

# Arousal peaks at 100s and decays across 101..108s with chat tail still active
loud = np.zeros(300, dtype=np.float32)
loud[99:106] = [0.5, 0.95, 0.85, 0.70, 0.55, 0.45, 0.10]
chat_rate = np.zeros(300, dtype=np.float32)
chat_rate[100:111] = 0.6
signals = {{"loud": loud, "chat_rate": chat_rate}}

# Case 1: Start moves EARLIER to clean sentence pause (~78s), end extends through reaction (~107+)
w1 = boundaries.story_window(100.0, words, signals, dur, min_len=20.0, max_len=60.0, category="funny")
assert w1["start"] < 82.0, f"Expected start earlier to pause, got {{w1}}"
assert w1["end"] >= 106.0, f"Expected end extended across reaction, got {{w1}}"

# Case 2: max_len clamping moves START ONLY, preserving reaction end
w_clamp_max = boundaries.story_window(100.0, words, signals, dur, min_len=10.0, max_len=18.0, category="funny")
assert w_clamp_max["end"] - w_clamp_max["start"] <= 18.2
assert abs(w_clamp_max["end"] - w1["end"]) <= 0.5, f"Reaction tail changed during max clamp: {{w_clamp_max}} vs {{w1}}"

# Case 3: min_len expansion moves START ONLY, preserving reaction end
quiet_signals = {{"loud": np.array([0.9 if i == 100 else 0.0 for i in range(300)], dtype=np.float32)}}
w_clamp_min = boundaries.story_window(100.0, words, quiet_signals, dur, min_len=35.0, max_len=60.0)
assert w_clamp_min["end"] - w_clamp_min["start"] >= 34.8
assert w_clamp_min["start"] <= w_clamp_min["end"] - 34.8

# Case 4: Pass-2 confident judge hints
w_hint = boundaries.story_window(
    100.0,
    words,
    signals,
    dur,
    min_len=15.0,
    max_len=60.0,
    hints={{"confident": True, "setup_start": 78.0, "reaction_end": 109.0}},
)
assert 75.0 <= w_hint["start"] <= 82.5
assert w_hint["end"] >= 106.0

# Case 5: boundary_priors.json override shifts clutch preroll earlier (~76s)
w_prior = boundaries.story_window(
    100.0,
    words,
    quiet_signals,
    dur,
    min_len=15.0,
    max_len=60.0,
    priors=r"{priors_path}",
    category="clutch",
)
assert w_prior["start"] <= 80.0

print(json.dumps({{"w1": w1, "w_clamp_max": w_clamp_max, "w_clamp_min": w_clamp_min, "w_hint": w_hint, "w_prior": w_prior}}))
"""
        res = _run_engine_script(script)
        assert res["w1"]["peak_sec"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 4. Candidate Fusion + 4-Hour (14,400s) Timeline Stress Test
# ---------------------------------------------------------------------------


def test_candidate_fusion_4h_timeline_stress() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_v2_4h_") as tmp:
        chat_path = Path(tmp) / "vod_4h.chat.jsonl"
        # Write 4,000 chat lines across 4 hours
        chat_lines = [
            json.dumps({"t": float(t), "author": f"u_{i % 300}", "text": "KEKW" if i % 5 == 0 else "wow", "kind": "message"})
            for i, t in enumerate(range(100, 14300, 3))
        ]
        chat_path.write_text("\n".join(chat_lines), encoding="utf-8")

        script = f"""
import json
import time
import numpy as np
import highlights
from v2 import candidates

dur = 14400.0
n = int(dur)
# Inject 30 distinct peaks well spread across 4 hours
peak_positions = list(range(300, 14100, 460))

def fake_motion(src, d, report):
    flow = np.ones(n, dtype=float) * 0.1
    cuts = np.zeros(n, dtype=float)
    for p in peak_positions:
        flow[p - 2 : p + 3] = 8.0
    return flow, cuts

def fake_audio(src):
    rms = np.ones(n, dtype=float) * 0.03
    hf = np.ones(n, dtype=float) * 0.08
    for p in peak_positions:
        rms[p - 2 : p + 3] = 0.85
        hf[p - 2 : p + 3] = 0.70
    return rms, hf

highlights._motion = fake_motion
highlights._audio = fake_audio

# Pure-NumPy peak finder check
sig = np.zeros(200, dtype=np.float64)
sig[30] = 0.9; sig[35] = 0.6; sig[100] = 0.85; sig[150] = 0.05
pks = candidates.find_peaks_1d(sig, distance=30, prominence=0.12)
assert pks == [30, 100], f"Unexpected peaks: {{pks}}"

t0 = time.perf_counter()
res = candidates.build_timeline_and_candidates(
    "dummy_4h.mp4",
    dur,
    count=8,
    length=40.0,
    data_dir=r"{tmp}",
    motion_on=True,
    chat_path=r"{chat_path}",
    enable_audio_events=False,
    profile="balanced",
)
elapsed = time.perf_counter() - t0
cands = res["candidates"]
pts = res["timeline_curve"]["points"]
starts = sorted(c["start"] for c in cands)
print(json.dumps({{
    "elapsed": round(elapsed, 4),
    "cand_count": len(cands),
    "curve_points": len(pts),
    "spread": round(starts[-1] - starts[0], 2) if starts else 0.0,
    "reasons_sample": cands[0]["reasons"] if cands else [],
}}))
"""
        res = _run_engine_script(script)
        assert res["elapsed"] < 2.0, f"4h fusion took {res['elapsed']}s (>= 2.0s)"
        assert 20 <= res["cand_count"] <= 40, f"Expected 20-40 candidates, got {res['cand_count']}"
        assert 100 <= res["curve_points"] <= 800
        assert res["spread"] >= 10000.0


# ---------------------------------------------------------------------------
# 5. DeepSeek judge_v2 Multimodal & Cross-Checks
# ---------------------------------------------------------------------------


def test_smart_judge_v2_multimodal_crosschecks_and_invalid_ts() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_v2_judge_") as tmp:
        script = """
import json
import urllib.request
import numpy as np
import llm
import smart

llm.save(True, "https://api.example.com/v1", "deepseek-v4-1-flash-economy-261017", "test-key-xyz")
captured = []
canned = []

class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
    def read(self, _n=-1):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False

def fake_urlopen(req, timeout=60):
    body = json.loads(req.data.decode("utf-8"))
    captured.append(body)
    return FakeResp({"choices": [{"message": {"content": json.dumps(canned.pop(0))}}]})

urllib.request.urlopen = fake_urlopen

# 1. Valid funny clip with laugh+chat corroboration -> 1.20x boost & valid timestamps
canned.append({
    "is_moment": True,
    "category": "funny",
    "on_screen": "Streamer drops controller laughing as chat spams KEKW.",
    "setup_start": "00:04",
    "payoff": "00:18",
    "reaction_end": "00:26",
    "hook_line": "HE DROPPED HIS CONTROLLER MID FIGHT?!",
    "title": "Controller Slip Disaster",
    "scores": {"hook": 7.5, "payoff_visible": 8.0, "standalone": 7.5, "emotion": 8.0, "clarity": 7.5},
    "verdict": "keep",
    "cut_reason": "none",
    "confidence": 0.88,
})
r_funny_boost = smart.judge_v2(
    "cloud:deepseek-v4-1-flash-economy-261017",
    "[00:04] Wait watch this!\\n[00:18] NO WAY!",
    window_start=100.0,
    window_end=135.0,
    p_rel=18.0,
    signal_strip="Signals around peak t=118s: laugh=0.72, chat_rate=0.81",
    chat_lines=["[00:19] viewer: KEKW"],
    images=[b"FAKE3X2JPEG"],
    signals={"laugh": np.array([0.8]*150), "chat_rate": np.array([0.8]*150)},
    p=118,
)

# Verify HTTP payload sent 3x2 image, [MM:SS] transcript, chat lines, signal strip, thinking: disabled
req_body = captured[0]
assert req_body["thinking"] == {"type": "disabled"}
assert req_body["max_tokens"] == 500
content = req_body["messages"][0]["content"]
assert isinstance(content, list)
assert any(p.get("type") == "image_url" for p in content)
prompt_txt = next(p["text"] for p in content if p.get("type") == "text")
assert "[00:04]" in prompt_txt
assert "[00:19] viewer: KEKW" in prompt_txt
assert "Signals around peak t=118s" in prompt_txt

# 2. Funny clip WITHOUT laugh or chat -> 0.60x demotion
canned.append({
    "is_moment": True,
    "category": "funny",
    "on_screen": "Walking quietly in lobby.",
    "setup_start": "00:05",
    "payoff": "00:15",
    "reaction_end": "00:20",
    "hook_line": "THIS LOBBY WALK WAS CRAZY?!",
    "title": "Quiet Walk",
    "scores": {"hook": 7.5, "payoff_visible": 8.0, "standalone": 7.5, "emotion": 8.0, "clarity": 7.5},
    "verdict": "keep",
    "cut_reason": "none",
    "confidence": 0.8,
})
r_funny_demote = smart.judge_v2(
    "cloud:deepseek-v4-1-flash-economy-261017",
    "[00:05] Look at this random walk over here.",
    window_start=100.0,
    window_end=135.0,
    p_rel=15.0,
    signals={"loud": np.array([0.5]*150), "laugh": np.zeros(150), "chat_rate": np.zeros(150)},
    p=115,
)

# 3. Menu/AFK verdict='cut' disqualification + invalid timestamps discarded
canned.append({
    "is_moment": False,
    "category": "rage",
    "on_screen": "Static main menu screen while yelling.",
    "setup_start": "00:25",
    "payoff": "00:10",  # invalid: payoff < setup_start!
    "reaction_end": "00:50",  # invalid: > window_dur
    "hook_line": "SCREAMING AT THE MAIN MENU",
    "title": "Static Menu",
    "scores": {"hook": 6, "payoff_visible": 2, "standalone": 3, "emotion": 8, "clarity": 4},
    "verdict": "cut",
    "cut_reason": "menu",
    "confidence": 0.9,
})
r_cut = smart.judge_v2(
    "cloud:deepseek-v4-1-flash-economy-261017",
    "[00:10] Why is this menu broken!",
    window_start=100.0,
    window_end=130.0,
    p_rel=10.0,
)

print(json.dumps({
    "funny_boost": r_funny_boost,
    "funny_demote": r_funny_demote,
    "cut": r_cut,
}))
"""
        res = _run_engine_script(script, data_dir=Path(tmp))
        assert res["funny_boost"]["score"] > res["funny_demote"]["score"]
        assert res["funny_boost"]["timestamps_valid"] is True
        assert res["funny_boost"]["confident"] is True
        assert res["funny_boost"]["setup_start"] == pytest.approx(4.0)
        assert res["cut"]["empty_scream"] is True
        assert res["cut"]["score"] <= 3.0
        assert res["cut"]["timestamps_valid"] is False
        assert res["cut"]["setup_start"] is None
        assert res["cut"]["confidence"] <= 0.3


# ---------------------------------------------------------------------------
# 6. pipeline_runner.py --mode scout & --mode export E2E on Real FFmpeg MP4
# ---------------------------------------------------------------------------


def test_pipeline_runner_scout_and_export_modes() -> None:
    with tempfile.TemporaryDirectory(prefix="clip_v2_runner_") as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "neon_data"
        out_dir = tmp_path / "exports"
        data_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        src_mp4 = _make_synthetic_mp4(tmp_path / "synthetic_vod.mp4", duration_sec=14.0)

        chat_jsonl = tmp_path / "synthetic_vod.chat.jsonl"
        chat_jsonl.write_text(
            "\n".join(
                json.dumps({"t": float(t), "author": f"ch_{i}", "text": "KEKW Pog", "kind": "message"})
                for i, t in enumerate([3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
            ),
            encoding="utf-8",
        )
        clips_json = tmp_path / "custom_clips.json"
        clips_json.write_text(
            json.dumps(
                [
                    {
                        "id": "custom_1",
                        "start": 2.0,
                        "end": 8.0,
                        "title": "Custom Cut One",
                        "hook": "WAIT FOR THIS CUT?!",
                        "visual_summary": "Synthetic test pattern moving.",
                        "score": 8.5,
                        "features": {"loud": 0.5, "chat": 0.6},
                    }
                ]
            ),
            encoding="utf-8",
        )

        script = f"""
import json
import sys
from pathlib import Path
import app
import captions
import facecam_detect
import pipeline_runner
import smart

# Stub ASR + VLM + facecam for fast deterministic offline verification while keeping real FFmpeg/NVENC export!
captions.transcribe = lambda src, s, e, data, cb, lang, model: {{
    "words": [
        {{"start": 0.5, "end": 1.1, "text": "Wait"}},
        {{"start": 1.2, "end": 1.8, "text": "for"}},
        {{"start": 1.9, "end": 2.5, "text": "this"}},
        {{"start": 2.6, "end": 3.4, "text": "play!"}},
    ],
    "language": "en",
    "model": model,
}}
captions._turbo_spec = lambda: "large-v3-turbo"
smart._model = lambda *a, **kw: "cloud:deepseek-v4-1-flash-economy-261017"
smart.judge_v2 = lambda *a, **kw: {{
    "score": 8.2,
    "judge_score": 0.82,
    "is_moment": True,
    "category": "clutch",
    "kind": "action",
    "visual_summary": "Fast color pattern shift.",
    "visual_action": 8.0,
    "setup_start": 0.5,
    "payoff": 3.5,
    "reaction_end": 6.0,
    "timestamps_valid": True,
    "confident": True,
    "confidence": 0.85,
    "hook_line": "WAIT FOR THIS CLUTCH?!",
    "hook": "WAIT FOR THIS CLUTCH?!",
    "title": "Synthetic Clutch",
    "verdict": "keep",
    "cut_reason": "none",
    "empty_scream": False,
    "focus_match": None,
}}
facecam_detect.detect = lambda *a, **kw: {{"found": False}}

# 1. --mode scout via CLI main() -> returns candidates + timeline_curve + facecam without exporting MP4s
scout_res = pipeline_runner.run_chunk_pipeline(
    r"{src_mp4}",
    data_dir=r"{data_dir}",
    mode="scout",
    chat_path=r"{chat_jsonl}",
    count=2,
    min_len=6.0,
    max_len=12.0,
    min_score=0.0,
)
mp4s_after_scout = list(Path(r"{out_dir}").glob("*.mp4"))

# 2. --mode export via CLI main() -> renders exact custom [2.0, 8.0] clip to out_dir
rc = pipeline_runner.main([
    "--source", r"{src_mp4}",
    "--output-dir", r"{out_dir}",
    "--data-dir", r"{data_dir}",
    "--mode", "export",
    "--clips-json", r"{clips_json}",
    "--no-cam-auto",
    "--layout", "blur",
])
assert rc == 0
exported_mp4s = list(Path(r"{out_dir}").glob("*.mp4"))
probed_dur = app.probe(exported_mp4s[0])["duration"] if exported_mp4s else 0.0

print(json.dumps({{
    "scout_ok": scout_res["ok"],
    "scout_mode": scout_res["mode"],
    "scout_candidates": len(scout_res["candidates"]),
    "curve_pts": len(scout_res["timeline_curve"]["points"]),
    "facecam_keys": sorted(scout_res["facecam"].keys()),
    "mp4s_after_scout": len(mp4s_after_scout),
    "exported_count": len(exported_mp4s),
    "exported_dur": round(float(probed_dur), 2),
}}))
"""
        res = _run_engine_script(script, data_dir=data_dir, timeout=180)
        assert res["scout_ok"] is True
        assert res["scout_mode"] == "scout"
        assert res["scout_candidates"] >= 1
        assert res["curve_pts"] >= 1
        assert "found" in res["facecam_keys"]
        assert res["mp4s_after_scout"] == 0
        assert res["exported_count"] == 1
        assert 5.0 <= res["exported_dur"] <= 7.0


# ---------------------------------------------------------------------------
# 7. LiveChatRecorder (IRC PRIVMSG, PING, Chunk Rotation)
# ---------------------------------------------------------------------------


def test_live_chat_recorder_irc_ping_and_rotation() -> None:
    assert parse_twitch_irc_line(":alice!alice@alice.tmi.twitch.tv PRIVMSG #tarik :KEKW what a shot") == (
        "alice",
        "KEKW what a shot",
    )
    assert (
        parse_twitch_irc_line("@badge-info=;color=#FF0000 :bob!bob@bob.tmi.twitch.tv PRIVMSG #tarik :LULW")
        == ("bob", "LULW")
    )
    assert format_twitch_pong("PING :tmi.twitch.tv") == "PONG :tmi.twitch.tv\r\n"

    with tempfile.TemporaryDirectory(prefix="clip_v2_irc_") as tmp:
        tmp_path = Path(tmp)
        chat1 = tmp_path / "chunk_001.chat.jsonl"
        chat2 = tmp_path / "chunk_002.chat.jsonl"

        received_from_client: list[str] = []
        server_done = threading.Event()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _fake_irc_server() -> None:
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(2.0)
                buf = ""
                while "JOIN #testchan" not in buf:
                    buf += conn.recv(1024).decode("utf-8", errors="replace")
                received_from_client.append(buf)
                conn.sendall(b"PING :tmi.twitch.tv\r\n")
                conn.sendall(b":viewer1!v1@v1.tmi.twitch.tv PRIVMSG #testchan :PogChamp\r\n")
                pong = conn.recv(1024).decode("utf-8", errors="replace")
                received_from_client.append(pong)
                server_done.wait(timeout=3.0)

        t = threading.Thread(target=_fake_irc_server, daemon=True)
        t.start()

        t0 = time.monotonic() - 12.5
        rec = LiveChatRecorder(
            "twitch",
            "testchan",
            chat1,
            chunk_start_monotonic=t0,
            irc_host="127.0.0.1",
            irc_port=port,
        )
        try:
            rec.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not (chat1.is_file() and chat1.read_text(encoding="utf-8").strip()):
                time.sleep(0.05)

            assert chat1.is_file()
            line1 = json.loads(chat1.read_text(encoding="utf-8").strip().splitlines()[0])
            assert line1["author"] == "viewer1"
            assert line1["text"] == "PogChamp"
            assert line1["kind"] == "message"
            assert line1["t"] >= 12.0

            # Rotate to chunk 2 resetting t to ~0.0
            rot_start = time.monotonic()
            rec.rotate_chunk(chat2, chunk_start_monotonic=rot_start)
            rec._write_message("viewer2", "New chunk hype", monotonic_ts=rot_start + 0.42)
            line2 = json.loads(chat2.read_text(encoding="utf-8").strip().splitlines()[0])
            assert line2["author"] == "viewer2"
            assert 0.0 <= line2["t"] <= 1.0
        finally:
            server_done.set()
            rec.stop(timeout=1.0)
            srv.close()
            t.join(timeout=2.0)

        joined = "".join(received_from_client)
        assert "JOIN #testchan" in joined
        assert "PONG :tmi.twitch.tv" in joined


# ---------------------------------------------------------------------------
# 8. Studio API E2E + Stress Test + RetentionGC Isolation
# ---------------------------------------------------------------------------


def test_studio_api_e2e_stress_and_safety_isolation(
    studio_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, tmp_path = studio_env
    src_mp4 = _make_synthetic_mp4(tmp_path / "source_studio.mp4", duration_sec=20.0)
    src_bytes = src_mp4.read_bytes()

    # Create a dummy raw chunk + expired GC target to verify RetentionGC never touches studio_dir
    raw_dir = Path(settings.raw_recordings_dir)
    raw_chunk = raw_dir / "tarik_chunk_001.mp4"
    shutil.copy2(src_mp4, raw_chunk)

    now = utcnow_iso()
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn = sqlite3.connect(settings.db_path)
    try:
        streamer_id = int(
            conn.execute(
                """
                INSERT INTO streamers (
                    name, slug, platform, channel_url, channel_ref,
                    auto_watch, clip_opts_json, is_live, created_at, updated_at
                )
                VALUES ('Tarik', 'tarik', 'twitch', 'https://twitch.tv/tarik', 'tarik', 1, '{}', 0, ?, ?)
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
                """
                INSERT INTO recording_chunks (
                    session_id, streamer_id, chunk_index, file_path,
                    status, delete_after, created_at
                )
                VALUES (?, ?, 1, ?, 'processed', ?, ?)
                """,
                (session_id, streamer_id, str(raw_chunk), old_ts, now),
            ).lastrowid
        )
        # Seed 4 prior boundary_feedback rows so exporting 2 adjusted clips pushes total to 6 (>= 5)
        for idx in range(4):
            conn.execute(
                """
                INSERT INTO boundary_feedback (
                    project_id, streamer_id, category, peak_sec,
                    orig_start, orig_end, final_start, final_end,
                    preroll_sec, tail_sec, created_at
                )
                VALUES (NULL, ?, 'clutch', 50.0, 35.0, 56.0, 33.0, 58.0, 17.0, 8.0, ?)
                """,
                (streamer_id, now),
            )
        conn.commit()
    finally:
        conn.close()

    from app.services.copywriter import CopywriterService
    from app.services.google_drive import GoogleDriveService
    from app.services.neon_client import NeonClippingClient

    async def fake_ascout_vod(self, source_path: str | Path, **_kw):
        return {
            "ok": True,
            "mode": "scout",
            "duration": 20.0,
            "facecam": {"found": True, "cam": {"x": 0.02, "y": 0.7, "w": 0.25, "h": 0.25}},
            "timeline_curve": {
                "duration": 20.0,
                "step_sec": 1.0,
                "points": [{"t": float(i), "hype": 0.1 * (i % 5), "audio": 0.2, "chat": 0.3, "laugh": 0.1} for i in range(20)],
            },
            "candidates": [
                {
                    "id": "c_01",
                    "start": 2.0,
                    "end": 10.0,
                    "peak_sec": 7.0,
                    "duration": 8.0,
                    "score": 88,
                    "llm": 8.4,
                    "visual_action": 8.0,
                    "visual_summary": "Clutch headshot in smoke.",
                    "category": "clutch",
                    "title": "Smoke Clutch",
                    "hook": "HOW DID HE HIT THAT?!",
                    "transcript": "No way I hit that!",
                    "reasons": [{"code": "chat_spike", "value": 0.85, "t": 7.0}],
                    "selected": True,
                    "orig_start": 2.0,
                    "orig_end": 10.0,
                },
                {
                    "id": "c_02",
                    "start": 11.0,
                    "end": 18.0,
                    "peak_sec": 15.0,
                    "duration": 7.0,
                    "score": 80,
                    "llm": 7.8,
                    "visual_action": 7.0,
                    "visual_summary": "Funny whiff reaction.",
                    "category": "funny",
                    "title": "Funny Whiff",
                    "hook": "WAIT FOR THE WHIFF...",
                    "transcript": "Oh no!",
                    "reasons": [{"code": "laughter", "value": 0.72, "t": 15.0}],
                    "selected": True,
                    "orig_start": 11.0,
                    "orig_end": 18.0,
                },
            ],
        }

    async def fake_aexport_vod_clips(self, source_path: str | Path, output_dir: str | Path, clips_json_path: str | Path, **_kw):
        clips_spec = json.loads(Path(clips_json_path).read_text(encoding="utf-8"))
        out = []
        for idx, c in enumerate(clips_spec, start=1):
            mp4 = Path(output_dir) / f"exported_{idx}.mp4"
            srt = Path(output_dir) / f"exported_{idx}.srt"
            shutil.copy2(src_mp4, mp4)
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
            out.append(
                {
                    "index": idx,
                    "start": c["start"],
                    "end": c["end"],
                    "duration": round(c["end"] - c["start"], 3),
                    "title": c["title"],
                    "hook": c.get("hook", ""),
                    "transcript": "No way I hit that!",
                    "visual_summary": c.get("visual_summary", ""),
                    "score": c.get("score", 85),
                    "features": c.get("features", {}),
                    "output_mp4": str(mp4),
                    "output_srt": str(srt),
                }
            )
        return {"ok": True, "mode": "export", "clips": out}

    async def fake_copy(self, **_kw):
        return {"title": "Studio Exported Short", "description": "Great play!", "tags": ["Shorts"], "source": "llm"}

    async def fake_drive_upload(self, *_args, **_kw):
        return None

    monkeypatch.setattr(NeonClippingClient, "ascout_vod", fake_ascout_vod)
    monkeypatch.setattr(NeonClippingClient, "aexport_vod_clips", fake_aexport_vod_clips)
    monkeypatch.setattr(CopywriterService, "generate_youtube_copy", fake_copy)
    monkeypatch.setattr(GoogleDriveService, "upload_clip", fake_drive_upload)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        # 1. Path-traversal rejection
        bad_init = client.post(
            "/api/studio/uploads/init",
            json={"filename": "../evil.mp4", "total_size": 100, "kind": "video"},
        )
        assert bad_init.status_code == 400
        bad_slash = client.post(
            "/api/studio/uploads/init",
            json={"filename": "sub/evil.mp4", "total_size": 100, "kind": "video"},
        )
        assert bad_slash.status_code == 400

        # 2. 12-chunk sequential upload stress test + exact byte match
        init_res = client.post(
            "/api/studio/uploads/init",
            json={"filename": "vod_test.mp4", "total_size": len(src_bytes), "kind": "video"},
        )
        assert init_res.status_code == 200
        upload_id = init_res.json()["upload_id"]

        n_chunks = 12
        chunk_len = (len(src_bytes) + n_chunks - 1) // n_chunks
        for i in range(n_chunks):
            part = src_bytes[i * chunk_len : (i + 1) * chunk_len]
            r_chunk = client.put(
                f"/api/studio/uploads/{upload_id}/chunk?index={i}",
                content=part,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert r_chunk.status_code == 200
            assert r_chunk.json()["bytes_written"] == len(part)

        comp_res = client.post(f"/api/studio/uploads/{upload_id}/complete")
        assert comp_res.status_code == 200
        uploaded_file = Path(comp_res.json()["file_path"])
        assert uploaded_file.is_file()
        assert uploaded_file.read_bytes() == src_bytes

        # 3. Create upload project -> Range 206 video + JPEG preview
        proj_res = client.post(
            "/api/studio/projects",
            json={
                "title": "Stress Test VOD",
                "source_type": "upload",
                "upload_id": upload_id,
                "streamer_id": streamer_id,
            },
        )
        assert proj_res.status_code == 200
        proj = proj_res.json()
        pid = proj["id"]
        assert proj["duration_sec"] == pytest.approx(20.0, abs=1.0)
        assert proj["has_preview"] is True

        r_vid = client.get(
            f"/api/studio/projects/{pid}/video",
            headers={"Range": "bytes=0-1023"},
        )
        assert r_vid.status_code == 206
        assert len(r_vid.content) == 1024
        assert r_vid.content == src_bytes[:1024]

        r_prev = client.get(f"/api/studio/projects/{pid}/preview")
        assert r_prev.status_code == 200
        assert r_prev.content[:3] == b"\xff\xd8\xff"

        # 4. POST /analyze -> poll until analyzed
        r_ana = client.post(f"/api/studio/projects/{pid}/analyze", json={"options": {"clip_count": 2}})
        assert r_ana.status_code == 200
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            cur_p = client.get(f"/api/studio/projects/{pid}").json()
            if cur_p["status"] in ("analyzed", "error"):
                break
            time.sleep(0.05)
        assert cur_p["status"] == "analyzed", f"Analyze failed: {cur_p}"
        assert len(cur_p["candidates"]) == 2
        assert len(cur_p["timeline_curve"]["points"]) == 20

        # 5. Stress test 40 rapid PATCH /candidates drag/resize updates (zero SQLite lock errors)
        base_cands = cur_p["candidates"]
        for step in range(40):
            s0 = round(1.0 + step * 0.05, 2)
            e0 = round(9.5 + step * 0.05, 2)
            s1 = round(10.5 + step * 0.02, 2)
            e1 = round(17.5 + step * 0.02, 2)
            patched = [
                {**base_cands[0], "start": s0, "end": e0},
                {**base_cands[1], "start": s1, "end": e1},
            ]
            r_patch = client.patch(
                f"/api/studio/projects/{pid}/candidates",
                json={"candidates": patched},
            )
            assert r_patch.status_code == 200, f"PATCH failed on step {step}: {r_patch.text}"

        final_p = client.get(f"/api/studio/projects/{pid}").json()
        assert final_p["candidates"][0]["start"] == pytest.approx(2.95)
        assert final_p["candidates"][0]["end"] == pytest.approx(11.45)
        assert final_p["candidates"][1]["start"] == pytest.approx(11.28)
        assert final_p["candidates"][1]["end"] == pytest.approx(18.28)

        # 6. Export -> clip_drafts + boundary_feedback + boundary_priors.json
        r_exp = client.post(f"/api/studio/projects/{pid}/export", json={})
        assert r_exp.status_code == 200
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            exp_p = client.get(f"/api/studio/projects/{pid}").json()
            if exp_p["status"] in ("exported", "error"):
                break
            time.sleep(0.05)
        assert exp_p["status"] == "exported", f"Export failed: {exp_p}"
        exported_ids = exp_p["options"]["last_exported_clip_ids"]
        assert len(exported_ids) == 2

        priors_file = Path(settings.neonclipping_data_dir) / "boundary_priors.json"
        assert priors_file.is_file()
        priors_data = json.loads(priors_file.read_text(encoding="utf-8"))
        assert priors_data["sample_count"] >= 5

        # 7. Create a chunk project too, delete upload project, verify RetentionGC & safety isolation
        chunk_proj = client.post(
            "/api/studio/projects",
            json={"source_type": "chunk", "chunk_id": chunk_id},
        )
        assert chunk_proj.status_code == 200
        chunk_pid = chunk_proj.json()["id"]

        # RetentionGC deletes expired raw_chunk, NEVER touches uploaded_file in studio_dir
        gc_res = asyncio.run(RetentionGC(settings).run_once())
        assert chunk_id in gc_res["deleted_chunk_ids"]
        assert uploaded_file.is_file()

        # Delete upload project removes upload & preview while leaving clip_drafts & drafts_dir MP4s intact
        draft_mp4s_before = sorted(p.name for p in Path(settings.drafts_dir).glob("*.mp4"))
        assert len(draft_mp4s_before) == 2
        del_res = client.delete(f"/api/studio/projects/{pid}")
        assert del_res.status_code == 200
        assert not uploaded_file.exists()
        assert not (Path(settings.studio_dir) / "previews" / f"project_{pid}.jpg").exists()
        draft_mp4s_after = sorted(p.name for p in Path(settings.drafts_dir).glob("*.mp4"))
        assert draft_mp4s_after == draft_mp4s_before
        assert list((Path(settings.studio_dir) / "_uploads").iterdir()) == []

        # Delete chunk project NEVER deletes anything in raw_recordings_dir or drafts_dir
        shutil.copy2(src_mp4, raw_chunk)
        del_chunk_proj = client.delete(f"/api/studio/projects/{chunk_pid}")
        assert del_chunk_proj.status_code == 200
        assert raw_chunk.is_file()

    conn_check = sqlite3.connect(settings.db_path)
    try:
        draft_count = conn_check.execute("SELECT COUNT(*) FROM clip_drafts").fetchone()[0]
        fb_count = conn_check.execute("SELECT COUNT(*) FROM boundary_feedback").fetchone()[0]
        assert draft_count == 2
        assert fb_count == 6
    finally:
        conn_check.close()


def test_studio_vod_url_import_streamlink_ytdlp_and_chat(
    studio_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test VOD URL allowlist, time offset parsing, Twitch Streamlink+fallback, chat, and cleanup."""
    from app.services.studio_service import StudioService, format_hms, parse_time_offset, parse_vod_url

    settings, tmp_root = studio_env

    assert parse_vod_url("https://www.twitch.tv/videos/2291234567")[0] == "twitch"
    assert parse_vod_url("https://youtu.be/dQw4w9WgXcQ")[0] == "youtube"
    assert parse_time_offset("01:02:03") == pytest.approx(3723.0)
    assert parse_time_offset("05:30") == pytest.approx(330.0)
    assert format_hms(3723) == "01:02:03"

    src_mp4 = _make_synthetic_mp4(tmp_root / "vod_sample.mp4", duration_sec=12.0)

    called: list[tuple[str, str, float | None, float | None]] = []

    async def fake_streamlink(self, url, dest_mp4, *, project_id, quality="1080p", start_sec=None, end_sec=None):
        called.append(("streamlink", url, start_sec, end_sec))
        dest_mp4.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_mp4, dest_mp4)

    async def fake_ytdlp(self, url, dest_mp4, *, project_id, quality="1080p", start_sec=None, end_sec=None):
        called.append(("yt-dlp", url, start_sec, end_sec))
        dest_mp4.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_mp4, dest_mp4)

    async def fake_chat(self, platform, url, dest_chat_jsonl, *, start_sec=None, end_sec=None):
        dest_chat_jsonl.write_text(
            json.dumps({"t": 4.2, "author": "viewer1", "text": "KEKW", "kind": "message"}) + "\n",
            encoding="utf-8",
        )
        return dest_chat_jsonl

    monkeypatch.setattr(StudioService, "_download_with_streamlink", fake_streamlink)
    monkeypatch.setattr(StudioService, "_download_with_ytdlp", fake_ytdlp)
    monkeypatch.setattr(StudioService, "_fetch_vod_chat", fake_chat)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as client:
        # Reject disallowed URLs & invalid time windows
        assert client.post("/api/studio/projects/from-url", json={"url": "file:///etc/passwd"}).status_code == 400
        assert client.post("/api/studio/projects/from-url", json={"url": "https://evil.example.com/v.mp4"}).status_code == 400
        assert (
            client.post(
                "/api/studio/projects/from-url",
                json={
                    "url": "https://www.twitch.tv/videos/123456",
                    "start_time": "01:00:00",
                    "end_time": "00:30:00",
                },
            ).status_code
            == 400
        )

        # Twitch URL with auto -> uses streamlink + fetches chat replay
        tw_res = client.post(
            "/api/studio/projects/from-url",
            json={
                "url": "https://www.twitch.tv/videos/2291234567",
                "downloader": "auto",
                "start_time": "00:10:00",
                "end_time": "00:40:00",
                "fetch_chat": True,
            },
        )
        assert tw_res.status_code == 200
        tw_id = tw_res.json()["id"]

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            tw_proj = client.get(f"/api/studio/projects/{tw_id}").json()
            if tw_proj["status"] in ("ready", "error"):
                break
            time.sleep(0.05)
        assert tw_proj["status"] == "ready", f"Twitch URL download failed: {tw_proj}"
        assert tw_proj["has_preview"] is True
        assert tw_proj["has_chat"] is True
        assert tw_proj["duration_sec"] == pytest.approx(12.0, abs=1.0)
        assert called[0] == ("streamlink", "https://www.twitch.tv/videos/2291234567", 600.0, 2400.0)

        # Range 206 video streaming works on downloaded VOD
        r_vid = client.get(f"/api/studio/projects/{tw_id}/video", headers={"Range": "bytes=0-511"})
        assert r_vid.status_code == 206
        assert len(r_vid.content) == 512

        # YouTube URL with auto -> uses yt-dlp
        yt_res = client.post(
            "/api/studio/projects/from-url",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "downloader": "auto"},
        )
        assert yt_res.status_code == 200
        yt_id = yt_res.json()["id"]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            yt_proj = client.get(f"/api/studio/projects/{yt_id}").json()
            if yt_proj["status"] in ("ready", "error"):
                break
            time.sleep(0.05)
        assert yt_proj["status"] == "ready"
        assert called[1][0] == "yt-dlp"

        # Delete cleans downloaded MP4 + chat + preview inside studio_dir
        dl_mp4 = Path(tw_proj["source_path"])
        assert dl_mp4.is_file()
        assert client.delete(f"/api/studio/projects/{tw_id}").status_code == 200
        assert not dl_mp4.exists()
        assert not dl_mp4.with_suffix(".chat.jsonl").exists()


def test_ytdlp_download_handles_large_cr_only_ffmpeg_progress(
    studio_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: >200 KB of \\r-only ffmpeg progress must not trigger asyncio LimitOverrunError."""
    from app.services import studio_service as ss_mod
    from app.services.studio_service import StudioService

    settings, tmp_root = studio_env
    src_mp4 = _make_synthetic_mp4(tmp_root / "cr_src.mp4", duration_sec=6.0)
    dest_mp4 = Path(settings.studio_dir) / "downloads" / "cr_test.mp4"

    # Build >150 KB of \r-delimited ffmpeg progress lines with zero \n characters
    cr_payload = (
        "".join(
            f"frame={i:6d} fps=120.0 q=-1.0 size={i*10}kB time=01:30:{i%60:02d}.50 bitrate=4500kbits/s speed=12.0x\r"
            for i in range(2000)
        )
    ).encode("utf-8")
    assert len(cr_payload) > 150_000 and b"\n" not in cr_payload

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(cr_payload)
            self.stdout.feed_eof()
            self.returncode: int | None = None

        async def wait(self) -> int:
            shutil.copy2(src_mp4, dest_mp4)
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    captured_cmd: list[str] = []

    async def fake_exec(*cmd, **_kw):
        captured_cmd.extend(str(x) for x in cmd)
        return _FakeProc()

    progress_updates: list[tuple[float, str]] = []

    async def fake_set_progress(self, project_id, *, progress, stage, status_value=None):
        progress_updates.append((progress, stage))

    monkeypatch.setattr(ss_mod, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(StudioService, "_set_project_progress", fake_set_progress)

    svc = StudioService(settings)
    asyncio.run(
        svc._download_with_ytdlp(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            dest_mp4,
            project_id=1,
            quality="720p",
            start_sec=0.0,
            end_sec=21600.0,
        )
    )
    assert dest_mp4.is_file()
    assert "-N" in captured_cmd and "8" in captured_cmd
    fmt_idx = captured_cmd.index("-f") + 1
    assert "height<=720" in captured_cmd[fmt_idx] and "fps>=50" in captured_cmd[fmt_idx]
    assert any("Downloading section" in stage for _, stage in progress_updates)



