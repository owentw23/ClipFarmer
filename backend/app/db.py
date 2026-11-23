from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
import aiosqlite

from fastapi import Depends

from app.config import Settings, get_settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL CHECK(platform IN ('youtube', 'twitch')),
    channel_url TEXT NOT NULL,
    channel_ref TEXT NOT NULL,
    auto_watch INTEGER NOT NULL DEFAULT 1,
    clip_opts_json TEXT NOT NULL,
    is_live INTEGER NOT NULL DEFAULT 0,
    hold_until_offline INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL UNIQUE,
    channel_title TEXT NOT NULL,
    email TEXT,
    oauth_provider TEXT NOT NULL DEFAULT 'google',
    encrypted_refresh_token TEXT,
    encrypted_access_token TEXT,
    token_expires_at TEXT,
    scopes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS streamer_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    youtube_account_id INTEGER NOT NULL REFERENCES youtube_accounts(id) ON DELETE CASCADE,
    default_privacy TEXT NOT NULL DEFAULT 'public',
    default_tags_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(streamer_id, youtube_account_id)
);

CREATE TABLE IF NOT EXISTS stream_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    external_stream_id TEXT,
    title TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'recording',
    end_reason TEXT,
    last_error TEXT,
    log_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recording_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    duration_sec REAL,
    merged_with_prev INTEGER NOT NULL DEFAULT 0,
    merged_from_chunk_id INTEGER REFERENCES recording_chunks(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'recorded',
    started_at TEXT,
    completed_at TEXT,
    delete_after TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clip_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES stream_sessions(id) ON DELETE SET NULL,
    chunk_id INTEGER REFERENCES recording_chunks(id) ON DELETE SET NULL,
    draft_number INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    file_path TEXT NOT NULL,
    thumbnail_path TEXT,
    drive_file_id TEXT,
    drive_url TEXT,
    start_sec REAL,
    end_sec REAL,
    duration_sec REAL,
    score REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    tg_chat_id TEXT,
    tg_message_id INTEGER,
    features_json TEXT,
    rating INTEGER,
    rating_source TEXT,
    rated_at TEXT,
    scheduled_publish_at TEXT,
    scheduled_privacy_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_draft_id INTEGER NOT NULL REFERENCES clip_drafts(id) ON DELETE CASCADE,
    youtube_account_id INTEGER REFERENCES youtube_accounts(id) ON DELETE SET NULL,
    youtube_video_id TEXT,
    youtube_url TEXT,
    privacy_status TEXT NOT NULL DEFAULT 'private',
    status TEXT NOT NULL DEFAULT 'pending',
    published_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    views INTEGER NOT NULL DEFAULT 0,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    estimated_minutes_watched REAL NOT NULL DEFAULT 0.0,
    engaged_views INTEGER NOT NULL DEFAULT 0,
    shares INTEGER NOT NULL DEFAULT 0,
    subscribers_gained INTEGER NOT NULL DEFAULT 0,
    subscribers_lost INTEGER NOT NULL DEFAULT 0,
    avg_view_duration_sec REAL NOT NULL DEFAULT 0.0,
    avg_view_percentage REAL NOT NULL DEFAULT 0.0,
    shorts_feed_views INTEGER NOT NULL DEFAULT 0,
    analytics_synced_at TEXT,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('upload', 'chunk')),
    source_path TEXT NOT NULL,
    chunk_id INTEGER REFERENCES recording_chunks(id) ON DELETE SET NULL,
    streamer_id INTEGER REFERENCES streamers(id) ON DELETE SET NULL,
    duration_sec REAL,
    preview_path TEXT,
    chat_path TEXT,
    options_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ready',
    progress REAL NOT NULL DEFAULT 0.0,
    stage TEXT NOT NULL DEFAULT '',
    timeline_json TEXT,
    candidates_json TEXT,
    facecam_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS boundary_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES studio_projects(id) ON DELETE SET NULL,
    streamer_id INTEGER REFERENCES streamers(id) ON DELETE SET NULL,
    category TEXT NOT NULL DEFAULT 'default',
    peak_sec REAL,
    orig_start REAL NOT NULL,
    orig_end REAL NOT NULL,
    final_start REAL NOT NULL,
    final_end REAL NOT NULL,
    preroll_sec REAL,
    tail_sec REAL,
    created_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


async def open_connection(db_path: str | Path | None = None) -> aiosqlite.Connection:
    """Open an aiosqlite connection configured with WAL mode and foreign keys."""
    target = Path(db_path or get_settings().db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(target))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA busy_timeout=15000;")
    return conn


async def _get_table_info(conn: aiosqlite.Connection, table_name: str) -> dict[str, aiosqlite.Row]:
    async with conn.execute(f"PRAGMA table_info({table_name});") as cur:
        rows = await cur.fetchall()
    return {row["name"]: row for row in rows}


async def _migrate_schema(conn: aiosqlite.Connection) -> None:
    """Idempotently migrate pre-existing Phase 1/2 databases to the current schema."""
    account_cols = await _get_table_info(conn, "youtube_accounts")
    if "oauth_provider" not in account_cols:
        await conn.execute(
            "ALTER TABLE youtube_accounts ADD COLUMN oauth_provider TEXT NOT NULL DEFAULT 'google';"
        )
        await conn.commit()

    chunk_cols = await _get_table_info(conn, "recording_chunks")
    chunk_migrations = (
        ("started_at", "ALTER TABLE recording_chunks ADD COLUMN started_at TEXT;"),
        ("completed_at", "ALTER TABLE recording_chunks ADD COLUMN completed_at TEXT;"),
        ("delete_after", "ALTER TABLE recording_chunks ADD COLUMN delete_after TEXT;"),
        (
            "merged_from_chunk_id",
            "ALTER TABLE recording_chunks ADD COLUMN merged_from_chunk_id INTEGER REFERENCES recording_chunks(id) ON DELETE SET NULL;",
        ),
        ("last_error", "ALTER TABLE recording_chunks ADD COLUMN last_error TEXT;"),
    )
    for col_name, alter_sql in chunk_migrations:
        if col_name not in chunk_cols:
            await conn.execute(alter_sql)
    await conn.commit()

    streamer_cols = await _get_table_info(conn, "streamers")
    if "hold_until_offline" not in streamer_cols:
        await conn.execute(
            "ALTER TABLE streamers ADD COLUMN hold_until_offline INTEGER NOT NULL DEFAULT 0;"
        )
    session_cols = await _get_table_info(conn, "stream_sessions")
    for col_name in ("end_reason", "last_error", "log_path"):
        if col_name not in session_cols:
            await conn.execute(f"ALTER TABLE stream_sessions ADD COLUMN {col_name} TEXT;")
    await conn.commit()

    clip_cols = await _get_table_info(conn, "clip_drafts")
    clip_migrations = (
        ("tg_chat_id", "ALTER TABLE clip_drafts ADD COLUMN tg_chat_id TEXT;"),
        ("tg_message_id", "ALTER TABLE clip_drafts ADD COLUMN tg_message_id INTEGER;"),
        ("features_json", "ALTER TABLE clip_drafts ADD COLUMN features_json TEXT;"),
        ("rating", "ALTER TABLE clip_drafts ADD COLUMN rating INTEGER;"),
        ("rating_source", "ALTER TABLE clip_drafts ADD COLUMN rating_source TEXT;"),
        ("rated_at", "ALTER TABLE clip_drafts ADD COLUMN rated_at TEXT;"),
        ("scheduled_publish_at", "ALTER TABLE clip_drafts ADD COLUMN scheduled_publish_at TEXT;"),
        ("scheduled_privacy_status", "ALTER TABLE clip_drafts ADD COLUMN scheduled_privacy_status TEXT;"),
    )
    for col_name, alter_sql in clip_migrations:
        if col_name not in clip_cols:
            await conn.execute(alter_sql)
    await conn.commit()

    metric_cols = await _get_table_info(conn, "metric_snapshots")
    metric_migrations = (
        (
            "engaged_views",
            "ALTER TABLE metric_snapshots ADD COLUMN engaged_views INTEGER NOT NULL DEFAULT 0;",
        ),
        (
            "shares",
            "ALTER TABLE metric_snapshots ADD COLUMN shares INTEGER NOT NULL DEFAULT 0;",
        ),
        (
            "subscribers_gained",
            "ALTER TABLE metric_snapshots ADD COLUMN subscribers_gained INTEGER NOT NULL DEFAULT 0;",
        ),
        (
            "subscribers_lost",
            "ALTER TABLE metric_snapshots ADD COLUMN subscribers_lost INTEGER NOT NULL DEFAULT 0;",
        ),
        (
            "avg_view_duration_sec",
            "ALTER TABLE metric_snapshots ADD COLUMN avg_view_duration_sec REAL NOT NULL DEFAULT 0.0;",
        ),
        (
            "avg_view_percentage",
            "ALTER TABLE metric_snapshots ADD COLUMN avg_view_percentage REAL NOT NULL DEFAULT 0.0;",
        ),
        (
            "shorts_feed_views",
            "ALTER TABLE metric_snapshots ADD COLUMN shorts_feed_views INTEGER NOT NULL DEFAULT 0;",
        ),
        (
            "analytics_synced_at",
            "ALTER TABLE metric_snapshots ADD COLUMN analytics_synced_at TEXT;",
        ),
    )
    for col_name, alter_sql in metric_migrations:
        if col_name not in metric_cols:
            await conn.execute(alter_sql)
    await conn.commit()

    target_cols = await _get_table_info(conn, "streamer_targets")
    privacy_col = target_cols.get("default_privacy")
    if privacy_col is not None and str(privacy_col["dflt_value"] or "").strip("'\"") != "public":
        # SQLite cannot alter a column's DEFAULT in place; rebuild the table preserving rows.
        await conn.execute("PRAGMA foreign_keys=OFF;")
        try:
            await conn.execute(
                """
                CREATE TABLE streamer_targets_migrated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
                    youtube_account_id INTEGER NOT NULL REFERENCES youtube_accounts(id) ON DELETE CASCADE,
                    default_privacy TEXT NOT NULL DEFAULT 'public',
                    default_tags_json TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(streamer_id, youtube_account_id)
                );
                """
            )
            await conn.execute(
                """
                INSERT INTO streamer_targets_migrated (
                    id, streamer_id, youtube_account_id, default_privacy,
                    default_tags_json, enabled, created_at
                )
                SELECT id, streamer_id, youtube_account_id, default_privacy,
                       default_tags_json, enabled, created_at
                FROM streamer_targets;
                """
            )
            await conn.execute("DROP TABLE streamer_targets;")
            await conn.execute("ALTER TABLE streamer_targets_migrated RENAME TO streamer_targets;")
            await conn.commit()
        finally:
            await conn.execute("PRAGMA foreign_keys=ON;")


async def init_db(db_path: str | Path | None = None) -> None:
    """Initialize SQLite WAL mode, create all schema tables, and apply migrations."""
    conn = await open_connection(db_path)
    try:
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        await _migrate_schema(conn)
    finally:
        await conn.close()


async def get_db(settings: Settings = Depends(get_settings)) -> AsyncIterator[aiosqlite.Connection]:
    """FastAPI dependency yielding an initialized database connection."""
    conn = await open_connection(settings.db_path)
    try:
        yield conn
    finally:
        await conn.close()
