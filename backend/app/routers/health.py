import inspect
from pathlib import Path
import shutil
import subprocess

import anyio
from fastapi import APIRouter, Depends
import aiosqlite

from app.config import Settings, get_settings
from app.db import get_db

router = APIRouter(prefix="/api", tags=["health"])


def _check_ffmpeg() -> dict:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return {"ok": False, "nvenc": False, "detail": "ffmpeg not found on PATH"}
    try:
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "nvenc": False,
                "detail": proc.stderr.strip() or f"ffmpeg exited with {proc.returncode}",
            }
        has_nvenc = "h264_nvenc" in proc.stdout
        return {
            "ok": True,
            "nvenc": has_nvenc,
            "detail": f"ffmpeg available ({ffmpeg_bin}); h264_nvenc={'yes' if has_nvenc else 'no'}",
        }
    except Exception as exc:
        return {"ok": False, "nvenc": False, "detail": f"ffmpeg check failed: {exc}"}


def _check_storage(settings: Settings) -> dict:
    root = Path(settings.clips_store_root)
    if not root.exists():
        return {"ok": False, "free_gb": 0.0, "root": str(root)}
    usage = shutil.disk_usage(root)
    free_gb = round(usage.free / (1024**3), 2)
    return {"ok": True, "free_gb": free_gb, "root": str(root)}


async def _check_neonclipping(settings: Settings) -> dict:
    base_url = settings.neonclipping_url
    data_dir = settings.neonclipping_data_dir
    try:
        from app.services.neon_client import NeonClippingClient
    except Exception as exc:
        return {
            "ok": False,
            "url": base_url,
            "detail": f"NeonClippingClient unavailable: {exc}",
        }

    try:
        client = NeonClippingClient(base_url, data_dir)
        result = await anyio.to_thread.run_sync(client.health)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return {
                "ok": bool(result.get("ok", False)),
                "url": str(result.get("url", base_url)),
                "detail": str(result.get("detail", "")),
            }
        return {"ok": bool(result), "url": base_url, "detail": str(result)}
    except Exception as exc:
        return {"ok": False, "url": base_url, "detail": f"health check failed: {exc}"}


def _check_llm(settings: Settings) -> dict:
    base_url = settings.llm_base_url.strip()
    model = settings.llm_model.strip()
    api_key = settings.llm_api_key.strip()
    is_coding_plan = "/api/coding/v3" in base_url
    configured = bool(base_url and model and api_key and not is_coding_plan)
    return {
        "ok": configured,
        "configured": configured,
        "base_url": base_url,
        "model": model,
    }


@router.get("/health")
async def get_health(
    db: aiosqlite.Connection = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Return service and dependency health checks."""
    try:
        async with db.execute("SELECT 1") as cur:
            await cur.fetchone()
        async with db.execute("PRAGMA journal_mode;") as cur:
            mode_row = await cur.fetchone()
        journal_mode = str(mode_row[0]).lower() if mode_row else "unknown"
        db_check = {"ok": True, "detail": f"sqlite connected (journal_mode={journal_mode})"}
    except Exception as exc:
        db_check = {"ok": False, "detail": f"database error: {exc}"}

    storage_check = _check_storage(settings)
    ffmpeg_check = await anyio.to_thread.run_sync(_check_ffmpeg)
    neon_check = await _check_neonclipping(settings)
    llm_check = _check_llm(settings)

    checks = {
        "database": db_check,
        "storage": storage_check,
        "ffmpeg": ffmpeg_check,
        "neonclipping": neon_check,
        "llm": llm_check,
    }
    all_ok = all(bool(check.get("ok")) for check in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
    }
