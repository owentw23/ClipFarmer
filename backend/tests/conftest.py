"""Keep every test away from the real Clips_Store, database, Telegram bot and GPU.

Settings come from the project .env, so a test that started the app without overriding
app.main.get_settings used to boot the real backend: the watcher, the recorder recovery,
the clip queue, retention cleanup and the Telegram poller, all on live data. These
environment variables win over .env, and they are set before anything imports the app.
Tests that build their own Settings(...) still use their own values.
"""
import os
from pathlib import Path
import tempfile

_SANDBOX = Path(tempfile.mkdtemp(prefix="clip_tests_"))
for _name in ("raw recordings", "drafts", "neon_tmp", "db"):
    (_SANDBOX / _name).mkdir(parents=True, exist_ok=True)

os.environ.update(
    {
        "ENABLE_BACKGROUND_WORKERS": "false",
        "CLIPS_STORE_ROOT": str(_SANDBOX),
        "RAW_RECORDINGS_DIR": str(_SANDBOX / "raw recordings"),
        "DRAFTS_DIR": str(_SANDBOX / "drafts"),
        "NEONCLIPPING_DATA_DIR": str(_SANDBOX / "neon_tmp"),
        "DB_PATH": str(_SANDBOX / "db" / "pipeline.sqlite3"),
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_ALLOWED_CHAT_IDS": "",
    }
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
