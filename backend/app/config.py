from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"


class Settings(BaseSettings):
    """Application configuration loaded from the project root .env file."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    clips_store_root: str = r"C:\Users\wirya\Documents\Clips_Store"
    raw_recordings_dir: str = r"C:\Users\wirya\Documents\Clips_Store\raw recordings"
    drafts_dir: str = r"C:\Users\wirya\Documents\Clips_Store\drafts"
    neonclipping_data_dir: str = r"C:\Users\wirya\Documents\Clips_Store\neon_tmp"
    db_path: str = r"C:\Users\wirya\Documents\Clips_Store\db\pipeline.sqlite3"

    neonclipping_port: int = 8770
    neonclipping_no_autoupdate: int = 1

    llm_base_url: str = "https://ark.ap-southeast.bytepluses.com/api/v3"
    llm_model: str = "deepseek-v4-1-flash-economy-261017"
    llm_api_key: str = ""

    neonclipping_llm_base_url: str = "https://ark.ap-southeast.bytepluses.com/api/v3"
    neonclipping_llm_model: str = "deepseek-v4-1-flash-economy-261017"
    neonclipping_llm_key: str = ""

    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/api/auth/google/callback"

    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""
    google_drive_redirect_uri: str = "http://127.0.0.1:8000/api/auth/google-drive/callback"

    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""

    token_encryption_key: str = ""
    next_public_api_base: str = "http://127.0.0.1:8000"

    enable_background_workers: bool = True
    watcher_poll_interval_sec: int = 60
    metrics_poll_interval_sec: int = 1800
    chunk_duration_seconds: int = 7200
    min_standalone_chunk_seconds: int = 3600
    chunk_retention_seconds: int = 7200
    vlm_min_score: float = 5.0
    gc_interval_seconds: int = 60
    # A recording whose file stops growing for this long is treated as stalled and restarted.
    recording_stall_seconds: int = 120
    # A partial recording at least this long is clipped when the recorder was stopped by
    # you, stalled or crashed (a stream that ends on its own still needs a full chunk).
    min_partial_seconds: int = 300
    learning_sync_interval_sec: int = 120
    youtube_cookies_file: str = ""
    frontend_base_url: str = "http://127.0.0.1:3000"

    @property
    def logs_dir(self) -> str:
        return str(Path(self.clips_store_root) / "logs")

    @property
    def studio_dir(self) -> str:
        return str(Path(self.clips_store_root) / "studio")

    @property
    def neonclipping_url(self) -> str:
        return f"http://127.0.0.1:{self.neonclipping_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
