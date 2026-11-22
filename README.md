# Clip Automation Pipeline (Phase 1)

Automated livestream recording, NeonClipping highlight extraction, and YouTube Shorts publishing pipeline.

## Runtime Storage Layout

All heavy media files and the SQLite database live **outside OneDrive** to prevent file-sync locks during recording and transcoding:

- `C:\Users\wirya\Documents\Clips_Store\raw recordings` — 60-minute raw `.mp4` stream chunks
- `C:\Users\wirya\Documents\Clips_Store\drafts` — rendered 9:16 vertical clip drafts
- `C:\Users\wirya\Documents\Clips_Store\neon_tmp` — NeonClipping working directory (`NEONCLIPPING_DATA_DIR`)
- `C:\Users\wirya\Documents\Clips_Store\db\pipeline.sqlite3` — SQLite database in WAL mode

## Phase 1 Setup

1. Review `.env` at the repository root (created from `.env.example` with an auto-generated Fernet `TOKEN_ENCRYPTION_KEY`).
   - Fill in `LLM_MODEL` and `LLM_API_KEY` (plus `NEONCLIPPING_LLM_MODEL` / `NEONCLIPPING_LLM_KEY`) using BytePlus ModelArk standard endpoint `https://ark.ap-southeast.bytepluses.com/api/v3`.
   - **Warning:** Do NOT use BytePlus ModelArk Coding Plan (`/api/coding/v3`) for non-coding automation.
   - Fill in `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and Telegram settings when enabling Phase 2–4 integrations.

2. Backend virtual environment (Python 3.12):

   ```powershell
   cd backend
   C:\Users\wirya\AppData\Local\Programs\Python\Python312\python.exe -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

## Running the Backend API

```powershell
cd backend
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000 --reload
```

Verify health:

```powershell
curl http://127.0.0.1:8000/api/health
```

## Running Tests

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -v
```
