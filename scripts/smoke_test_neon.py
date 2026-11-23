"""End-to-end smoke test for the local NeonClipping CUDA & NVENC runtime.

Run with:
    engines\\neonclipping\\.venv\\Scripts\\python.exe scripts\\smoke_test_neon.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / "engines" / "neonclipping"
DATA_DIR = Path(os.environ.get("NEONCLIPPING_DATA_DIR", r"C:\Users\wirya\Documents\Clips_Store\neon_tmp"))
PORT = int(os.environ.get("NEONCLIPPING_PORT", "8770"))

if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))


def _banner(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _http_get_json(url: str, timeout: float = 5.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="neon_smoke_"))
    synthetic_mp4 = tmp_dir / "synthetic_1080p.mp4"
    nvenc_test_mp4 = tmp_dir / "nvenc_check.mp4"
    server_proc: subprocess.Popen[str] | None = None

    try:
        # 1. Generate synthetic 15s 1920x1080 MP4 (testsrc2 moving pattern + sine audio)
        _banner(f"Generating 15s 1920x1080 synthetic MP4 at {synthetic_mp4} ...")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1920x1080:rate=30:duration=15",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100:duration=15",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(synthetic_mp4),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert synthetic_mp4.exists() and synthetic_mp4.stat().st_size > 10_000

        # 2. Verify h264_nvenc test encode succeeds (use static_ffmpeg if system ffmpeg requires newer NVENC API)
        _banner("Verifying h264_nvenc test encode ...")
        nvenc_cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-t",
            "2",
            "-i",
            str(synthetic_mp4),
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-cq",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(nvenc_test_mp4),
        ]
        res_nvenc = subprocess.run(nvenc_cmd, capture_output=True, text=True, timeout=60)
        if res_nvenc.returncode != 0:
            _banner(
                f"System ffmpeg ({shutil.which('ffmpeg')}) h264_nvenc failed: "
                f"{res_nvenc.stderr.strip().splitlines()[-1] if res_nvenc.stderr.strip() else res_nvenc.returncode}; "
                "activating static_ffmpeg (bundled in requirements.txt) ..."
            )
            import static_ffmpeg

            static_ffmpeg.add_paths()
            res_nvenc = subprocess.run(nvenc_cmd, check=True, capture_output=True, text=True, timeout=60)
        assert nvenc_test_mp4.exists() and nvenc_test_mp4.stat().st_size > 5_000
        _banner(f"h264_nvenc encode OK using {shutil.which('ffmpeg')}")

        # 3. Verify ctranslate2 CUDA device count > 0
        _banner("Checking ctranslate2 CUDA device count ...")
        import ctranslate2

        cuda_count = ctranslate2.get_cuda_device_count()
        _banner(f"ctranslate2.get_cuda_device_count() = {cuda_count}")
        assert cuda_count > 0, "Expected at least 1 CUDA GPU for ctranslate2"

        # 4. Verify streamlink imports cleanly
        _banner("Checking streamlink import ...")
        import streamlink  # noqa: F401

        _banner(f"streamlink {getattr(streamlink, '__version__', 'ok')} imported cleanly")

        # 5. Verify faster-whisper loads both 'base' and large-v3-turbo (captions._turbo_spec()) on CUDA
        _banner("Loading faster-whisper 'base' and large-v3-turbo on CUDA ...")
        import compat  # noqa: F401 - applies PyAV/subprocess shims
        import captions
        import numpy as np
        from faster_whisper import WhisperModel

        # Add NVIDIA pip wheel DLL dirs on Windows just like captions._load()
        site_nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
        for sub in ("cublas", "cudnn"):
            bin_dir = site_nvidia / sub / "bin"
            if bin_dir.exists():
                try:
                    os.add_dll_directory(str(bin_dir))
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]

        silence = np.zeros(16000, dtype="float32")
        models_dir = str(DATA_DIR / "models")

        m_base = WhisperModel("base", device="cuda", compute_type="int8_float16", download_root=models_dir)
        list(m_base.transcribe(silence)[0])
        _banner("Whisper 'base' CUDA load & smoke transcribe OK")
        del m_base

        turbo_spec = captions._turbo_spec()
        _banner(f"Resolved captions._turbo_spec() -> {turbo_spec}")
        assert turbo_spec != "small", (
            "captions._turbo_spec() fell back to 'small'; large-v3-turbo snapshot did not download"
        )
        m_turbo = WhisperModel(turbo_spec, device="cuda", compute_type="int8_float16", download_root=models_dir)
        list(m_turbo.transcribe(silence)[0])
        _banner("Whisper 'large-v3-turbo' CUDA load & smoke transcribe OK")
        del m_turbo

        # 6. Verify pipeline_runner overlap filter helper
        import pipeline_runner

        assert pipeline_runner.overlaps_excluded(10.0, 40.0, [(15.0, 35.0)]) is True
        assert pipeline_runner.overlaps_excluded(10.0, 40.0, [(38.0, 70.0)]) is False
        _banner("pipeline_runner overlap filter check OK")

        # 7. Boot NeonClipping app.py on port 8770 and verify /healthz & /readiness
        _banner(f"Starting NeonClipping app.py on port {PORT} ...")
        env = os.environ.copy()
        env["NEONCLIPPING_DATA_DIR"] = str(DATA_DIR)
        env["NEONCLIPPING_NO_AUTOUPDATE"] = "1"
        env["NEONCLIPPING_PORT"] = str(PORT)

        server_proc = subprocess.Popen(
            [sys.executable, str(ENGINE_DIR / "app.py")],
            cwd=str(ENGINE_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.monotonic() + 30.0
        health_data = None
        while time.monotonic() < deadline:
            if server_proc.poll() is not None:
                out = server_proc.stdout.read() if server_proc.stdout else ""
                raise RuntimeError(
                    f"NeonClipping app.py exited prematurely ({server_proc.returncode}): {out[-1000:]}"
                )
            try:
                status, health_data = _http_get_json(f"http://127.0.0.1:{PORT}/healthz")
                if status == 200 and health_data.get("ok"):
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                time.sleep(0.5)
        else:
            raise TimeoutError(f"NeonClipping /healthz did not return 200 ok within 30s on port {PORT}")

        r_status, readiness_data = _http_get_json(f"http://127.0.0.1:{PORT}/readiness", timeout=15.0)
        assert r_status == 200 and readiness_data.get("ok") is True, f"Unexpected /readiness: {readiness_data}"
        _banner(f"GET /healthz -> 200 {health_data}")
        _banner(f"GET /readiness -> 200 ok={readiness_data.get('ok')} items={len(readiness_data.get('items', []))}")

        _banner("ALL SMOKE TESTS PASSED")
        return 0
    finally:
        if server_proc is not None and server_proc.poll() is None:
            _banner("Terminating temporary NeonClipping server ...")
            server_proc.terminate()
            try:
                server_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server_proc.kill()
                server_proc.wait(timeout=5)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
