from datetime import datetime
from pathlib import Path
import re

from app.config import Settings, get_settings


def ensure_storage_dirs(settings: Settings | None = None) -> dict[str, Path]:
    """Ensure all non-OneDrive Clips_Store runtime directories exist."""
    cfg = settings or get_settings()
    paths = {
        "root": Path(cfg.clips_store_root),
        "raw_recordings": Path(cfg.raw_recordings_dir),
        "drafts": Path(cfg.drafts_dir),
        "previews": Path(cfg.clips_store_root) / "previews",
        "neon_tmp": Path(cfg.neonclipping_data_dir),
        "db_dir": Path(cfg.db_path).parent,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def ensure_studio_dir(settings: Settings | None = None) -> Path:
    """Ensure Studio VOD storage directories exist under Clips_Store\\studio."""
    cfg = settings or get_settings()
    studio_root = Path(cfg.studio_dir)
    for sub in (
        studio_root,
        studio_root / "uploads",
        studio_root / "_uploads",
        studio_root / "previews",
    ):
        sub.mkdir(parents=True, exist_ok=True)
    return studio_root


def resolve_safe_path(raw_path: str | Path, *allowed_roots: str | Path) -> Path:
    """Resolve raw_path and verify it stays inside at least one allowed root directory."""
    if not raw_path or not str(raw_path).strip():
        raise ValueError("Path must not be empty")
    resolved = Path(str(raw_path)).resolve()
    for root in allowed_roots:
        if resolved.is_relative_to(Path(root).resolve()):
            return resolved
    raise ValueError(f"Path {resolved} escapes allowed directories")


def streamer_preview_path(settings: Settings | None, streamer_id: int) -> Path:
    """Return the deterministic JPEG preview screenshot path for a streamer."""
    cfg = settings or get_settings()
    return Path(cfg.clips_store_root) / "previews" / f"streamer_{int(streamer_id)}.jpg"


def slugify_streamer(name: str) -> str:
    """Convert a streamer display name into a filesystem-safe lowercase slug."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return cleaned or "streamer"


def raw_chunk_filename(streamer_slug: str, dt: datetime, chunk_index: int) -> str:
    """Build canonical raw recording chunk filename: {slug}_{YYYY-MM-DD}_{HHMMSS}_chunk{NN:02d}.mp4."""
    slug = slugify_streamer(streamer_slug)
    date_part = dt.strftime("%Y-%m-%d")
    time_part = dt.strftime("%H%M%S")
    return f"{slug}_{date_part}_{time_part}_chunk{chunk_index:02d}.mp4"


def draft_clip_filename(
    streamer_slug: str,
    dt: datetime,
    chunk_index: int,
    draft_number: int,
    ext: str = "mp4",
) -> str:
    """Build canonical draft clip filename: {slug}_{YYYY-MM-DD}_{HHMMSS}_chunk{NN:02d}_draft_{draft_number}.{ext}."""
    slug = slugify_streamer(streamer_slug)
    date_part = dt.strftime("%Y-%m-%d")
    time_part = dt.strftime("%H%M%S")
    clean_ext = ext.lstrip(".") or "mp4"
    return f"{slug}_{date_part}_{time_part}_chunk{chunk_index:02d}_draft_{draft_number}.{clean_ext}"
