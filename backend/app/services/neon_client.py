"""Backend client for the local NeonClipping runtime and chunk pipeline."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import httpx

DEFAULT_NEON_URL = "http://127.0.0.1:8770"
DEFAULT_DATA_DIR = Path(r"C:\Users\wirya\Documents\Clips_Store\neon_tmp")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ENGINE_DIR = _REPO_ROOT / "engines" / "neonclipping"


class NeonClippingClient:
    """Client for talking to NeonClipping's HTTP API and local turbo pipeline."""

    def __init__(
        self,
        base_url: str = DEFAULT_NEON_URL,
        data_dir: str | Path | None = None,
        engine_dir: str | Path | None = None,
        python_executable: str | Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_NEON_URL).rstrip("/")
        raw_data = data_dir or os.environ.get("NEONCLIPPING_DATA_DIR") or DEFAULT_DATA_DIR
        self.data_dir = Path(raw_data)
        self.engine_dir = Path(engine_dir) if engine_dir else _DEFAULT_ENGINE_DIR
        self.python_executable = (
            Path(python_executable)
            if python_executable
            else (self.engine_dir / ".venv" / "Scripts" / "python.exe")
        )
        self.timeout = float(timeout)

    def health(self) -> dict[str, Any]:
        """Synchronously check NeonClipping /healthz and return normalized status."""
        url = f"{self.base_url}/healthz"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "url": self.base_url,
                    "detail": f"HTTP {resp.status_code} from /healthz",
                }
            data = resp.json()
            ok = bool(data.get("ok"))
            detail = (
                f"engine={data.get('engine', 'unknown')}, ffmpeg={bool(data.get('ffmpeg'))}"
            )
            return {"ok": ok, "url": self.base_url, "detail": detail}
        except Exception as exc:
            return {
                "ok": False,
                "url": self.base_url,
                "detail": f"unreachable ({type(exc).__name__}: {exc})",
            }

    async def ahealth(self) -> dict[str, Any]:
        """Asynchronously check NeonClipping /healthz and return normalized status."""
        url = f"{self.base_url}/healthz"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "url": self.base_url,
                    "detail": f"HTTP {resp.status_code} from /healthz",
                }
            data = resp.json()
            ok = bool(data.get("ok"))
            detail = (
                f"engine={data.get('engine', 'unknown')}, ffmpeg={bool(data.get('ffmpeg'))}"
            )
            return {"ok": ok, "url": self.base_url, "detail": detail}
        except Exception as exc:
            return {
                "ok": False,
                "url": self.base_url,
                "detail": f"unreachable ({type(exc).__name__}: {exc})",
            }

    def readiness(self) -> dict[str, Any]:
        """Fetch /readiness diagnostics from the running NeonClipping server."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/readiness")
            resp.raise_for_status()
            return resp.json()

    async def areadiness(self) -> dict[str, Any]:
        """Async /readiness diagnostics from the running NeonClipping server."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/readiness")
            resp.raise_for_status()
            return resp.json()

    def submit_chunk(
        self,
        chunk_path: str | Path,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upload a local chunk MP4 to NeonClipping and start a /make job."""
        src = Path(chunk_path)
        if not src.is_file():
            raise FileNotFoundError(f"Chunk file does not exist: {src}")

        with httpx.Client(timeout=max(self.timeout, 120.0)) as client:
            with src.open("rb") as fh:
                up_resp = client.post(
                    f"{self.base_url}/upload",
                    files={"file": (src.name, fh, "video/mp4")},
                )
            up_resp.raise_for_status()
            upload_info = up_resp.json()
            source_id = upload_info["id"]

            payload = {"id": source_id, **(options or {})}
            make_resp = client.post(f"{self.base_url}/make", json=payload)
            make_resp.raise_for_status()
            job_info = make_resp.json()

        return {
            "job": job_info["job"],
            "source_id": source_id,
            "upload": upload_info,
        }

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Fetch current status for a NeonClipping /make or /export job."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/make/{job_id}")
            if resp.status_code == 404:
                resp = client.get(f"{self.base_url}/status/{job_id}")
            resp.raise_for_status()
            return resp.json()

    def poll_job(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        timeout: float = 3600.0,
    ) -> dict[str, Any]:
        """Poll a NeonClipping job until status reaches 'done' or 'error'."""
        deadline = time.monotonic() + float(timeout)
        while True:
            status_payload = self.get_job_status(job_id)
            state = status_payload.get("status")
            if state in ("done", "error"):
                return status_payload
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for NeonClipping job {job_id} (last={state})"
                )
            time.sleep(poll_interval)

    def cleanup_source(
        self,
        source_id: str,
        job_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Remove source chunk and temporary artifacts for source_id in data_dir."""
        if not re.fullmatch(r"[A-Za-z0-9]+", str(source_id or "")):
            raise ValueError(f"source_id must be alphanumeric, got: {source_id!r}")
        if not self.data_dir.exists():
            return {
                "ok": True,
                "source_id": source_id,
                "data_dir": str(self.data_dir),
                "removed": [],
            }

        removed: list[str] = []
        patterns = [
            f"{source_id}.mp4",
            f"{source_id}.request.json",
            f"{source_id}.state.json",
            f"{source_id}.state.tmp",
            f"{source_id}.audio.*",
            f"{source_id}-*.words.json",
            f"{source_id}-*.wav",
        ]
        for pat in patterns:
            for item in self.data_dir.glob(pat):
                if item.is_file():
                    item.unlink(missing_ok=True)
                    removed.append(item.name)

        for folder_name in (source_id, f"{source_id}-preview"):
            folder = self.data_dir / folder_name
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)

        for jid in job_ids or ():
            if not re.fullmatch(r"[A-Za-z0-9]+", str(jid or "")):
                continue
            for ext in (".mp4", ".json", ".ass", ".srt", ".vtt", ".state.json", ".state.tmp"):
                p = self.data_dir / f"{jid}{ext}"
                if p.is_file():
                    p.unlink(missing_ok=True)
                    removed.append(p.name)

        return {
            "ok": True,
            "source_id": source_id,
            "data_dir": str(self.data_dir),
            "removed": removed,
        }

    def _build_turbo_invocation(
        self,
        chunk_path: str | Path,
        output_dir: str | Path | None = None,
        *,
        count: int = 3,
        min_len: float = 30.0,
        max_len: float = 60.0,
        cam_auto: bool = True,
        layout: str = "stack",
        fallback_layout: str = "blur",
        caption_style: str = "hormozi",
        caption_anim: str = "highlight",
        language: str | None = None,
        profile: str = "balanced",
        focus_brief: str = "",
        exclude_intervals: Sequence[tuple[float, float]] | None = None,
        cam: dict[str, float] | None = None,
        top: float | None = None,
        min_score: float | None = None,
        cleanup: bool = True,
        remove_staged_exports: bool = True,
        mode: str = "full",
        chat_path: str | Path | None = None,
        boundary_priors_path: str | Path | None = None,
        clips_json_path: str | Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        """Validate the engine install and build the pipeline_runner.py command + environment."""
        runner = self.engine_dir / "pipeline_runner.py"
        if not runner.is_file():
            raise FileNotFoundError(f"pipeline_runner.py not found at {runner}")
        if not self.python_executable.is_file():
            raise FileNotFoundError(
                f"NeonClipping Python executable not found at {self.python_executable}"
            )

        cmd = [
            str(self.python_executable),
            str(runner),
            "--source",
            str(Path(chunk_path).resolve()),
            "--data-dir",
            str(self.data_dir),
            "--count",
            str(count),
            "--min-len",
            str(min_len),
            "--max-len",
            str(max_len),
            "--layout",
            layout,
            "--fallback-layout",
            fallback_layout,
            "--caption-style",
            caption_style,
            "--caption-anim",
            caption_anim,
            "--profile",
            profile,
        ]
        if mode:
            cmd.extend(["--mode", str(mode)])
        if output_dir is not None:
            cmd.extend(["--output-dir", str(Path(output_dir).resolve())])
        if chat_path is not None:
            cmd.extend(["--chat-path", str(Path(chat_path).resolve())])
        if boundary_priors_path is not None:
            cmd.extend(["--boundary-priors-json", str(Path(boundary_priors_path).resolve())])
        if clips_json_path is not None:
            cmd.extend(["--clips-json", str(Path(clips_json_path).resolve())])
        if language:
            cmd.extend(["--language", language])
        if focus_brief:
            cmd.extend(["--focus-brief", focus_brief])
        if exclude_intervals:
            cmd.extend(
                [
                    "--exclude-intervals-json",
                    json.dumps([[float(a), float(b)] for a, b in exclude_intervals]),
                ]
            )
        if cam is not None:
            cmd.extend(["--cam-json", json.dumps(cam)])
        if top is not None:
            cmd.extend(["--top", str(float(top))])
        if min_score is not None:
            cmd.extend(["--min-score", str(float(min_score))])
        if not cam_auto:
            cmd.append("--no-cam-auto")
        if not cleanup:
            cmd.append("--no-cleanup")
        if remove_staged_exports and output_dir is not None:
            cmd.append("--remove-staged-exports")

        env = os.environ.copy()
        env["NEONCLIPPING_DATA_DIR"] = str(self.data_dir)
        env["NEONCLIPPING_NO_AUTOUPDATE"] = "1"
        if extra_env:
            for key, val in extra_env.items():
                if val is not None:
                    env[str(key)] = str(val)
        # pipeline_runner prints its result with ensure_ascii=False. Without this, a
        # non-ASCII clip title fails to encode on a Windows cp1252 pipe after all the GPU work.
        env["PYTHONIOENCODING"] = "utf-8"
        return cmd, env

    @staticmethod
    def _parse_runner_output(returncode: int | None, stdout: str, stderr: str) -> dict[str, Any]:
        stdout = stdout or ""
        stderr = stderr or ""
        if returncode != 0:
            raise RuntimeError(
                f"pipeline_runner failed (exit {returncode}): "
                f"{stderr[-800:] or stdout[-800:]}"
            )
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("pipeline_runner produced no JSON output")
        return json.loads(lines[-1])

    def refine_and_export_with_turbo(
        self,
        chunk_path: str | Path,
        output_dir: str | Path | None = None,
        *,
        timeout: float = 3600.0,
        **opts: Any,
    ) -> dict[str, Any]:
        """Blocking run of NeonClipping's pipeline_runner.py (scout + turbo refine + 9:16 export)."""
        cmd, env = self._build_turbo_invocation(chunk_path, output_dir, **opts)
        proc = subprocess.run(
            cmd,
            cwd=str(self.engine_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return self._parse_runner_output(proc.returncode, proc.stdout, proc.stderr)

    async def arefine_and_export_with_turbo(
        self,
        chunk_path: str | Path,
        output_dir: str | Path | None = None,
        *,
        extra_env: dict[str, str] | None = None,
        timeout: float = 3600.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run pipeline_runner.py without blocking the event loop.

        If the awaiting task is cancelled (for example at backend shutdown) or the timeout
        expires, the runner's whole process tree (Python, ffmpeg, CUDA workers) is killed.
        Otherwise it would keep holding the GPU, and the next startup would re-queue the
        same chunk next to it.
        """
        cmd, env = self._build_turbo_invocation(
            chunk_path, output_dir, extra_env=extra_env, **kwargs
        )
        popen_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.engine_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_kwargs,
        )
        # Popen plus a worker thread works on both Proactor and Selector event loops.
        comm = asyncio.ensure_future(asyncio.to_thread(proc.communicate))
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(comm), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc, comm)
            raise TimeoutError(
                f"pipeline_runner timed out after {timeout:.0f}s and was killed"
            ) from None
        except asyncio.CancelledError:
            await _kill_and_reap(proc, comm)
            raise
        return self._parse_runner_output(proc.returncode, stdout, stderr)

    async def ascout_vod(
        self,
        source_path: str | Path,
        *,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run pipeline_runner.py in 'scout' mode to return timeline curve and clip candidates."""
        return await self.arefine_and_export_with_turbo(
            source_path,
            output_dir=None,
            extra_env=extra_env,
            mode="scout",
            **kwargs,
        )

    async def aexport_vod_clips(
        self,
        source_path: str | Path,
        output_dir: str | Path,
        clips_json_path: str | Path,
        *,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run pipeline_runner.py in 'export' mode on custom user-adjusted clip regions."""
        return await self.arefine_and_export_with_turbo(
            source_path,
            output_dir=output_dir,
            extra_env=extra_env,
            mode="export",
            clips_json_path=clips_json_path,
            **kwargs,
        )


def kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Forcefully kill a process and all of its children."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


async def _kill_and_reap(
    proc: subprocess.Popen[Any],
    comm: asyncio.Future[Any],
    grace: float = 15.0,
) -> None:
    """Kill the runner tree, then give the communicate() thread a moment to drain pipes."""
    await asyncio.to_thread(kill_process_tree, proc)
    await asyncio.wait({comm}, timeout=grace)
    if not comm.done():
        comm.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
