# Clip Pipeline — Progress & Handoff

Last updated: 2026-11-22. Phases 1–5 are complete, plus a round of reliability fixes and a full UI redesign (Dusk theme). The system is in its first live test (one batch). This file is the source of truth for architecture, what has been done, and what comes next.

---

## 1. What the system does

Watches Twitch and YouTube streamers, records their live streams in 60-minute chunks, cuts three vertical Shorts from every chunk with a local CUDA fork of **NeonClipping**, writes a title and description with **BytePlus ModelArk**, stages the drafts on a dedicated **Google Drive**, asks for approval in **Telegram**, publishes approved clips to mapped **YouTube** channels, and tracks their performance. Channel scope: YouTube Shorts only (Instagram Reels deferred).

---

## 2. Architecture

### 2.1 Layout

| Part | Stack | Role |
| --- | --- | --- |
| `backend/` | FastAPI, aiosqlite (SQLite WAL), Python 3.12 venv | REST API plus every background worker, all started in `app/main.py` `lifespan` |
| `engines/neonclipping/` | Third-party clipper (faster-whisper CUDA, NVENC, YuNet), own Python 3.12 venv, gitignored | Highlight finding and 9:16 rendering, driven by `pipeline_runner.py` |
| `frontend/` | Next.js 16 + React 19 + Tailwind v4 + shadcn/ui (base-ui) + Recharts + `motion` | Dashboard UI, talks to `http://127.0.0.1:8000` through `src/lib/api.ts` |
| `C:\Users\wirya\Documents\Clips_Store` | Outside OneDrive (avoids sync locks) | `raw recordings\`, `drafts\`, `neon_tmp\` (engine scratch, also where learned weights would live), `db\pipeline.sqlite3` |
| `scripts/` | | `setup_neonclipping.ps1`, `smoke_test_neon.py` |

### 2.2 Pipeline (one object per stage, wired by callbacks in `main.py`)

```
WatcherManager ──on_online / on_offline──▶ RecorderManager ──on_chunk_queued──▶ ProcessingQueueWorker
 (poll every 60s)     (request_stop: non-blocking)  (streamlink | ffmpeg segment)       (one GPU job at a time)
                                                                                         │ on_chunk_drafts_ready
RetentionGC (every 60s) ◀── recording_chunks                                            ▼
MetricsCollector (every 30 min) ◀── publications ◀── YouTubePublisher ◀── TelegramBotService (long-poll + background publish jobs)
```

1. **Watch** (`services/watcher_manager.py`, `twitch_watcher.py`, `youtube_watcher.py`). Every 60s, every streamer with auto-watch on is checked:
   - Twitch: Helix `/streams` in batches of 100 when `TWITCH_CLIENT_ID`/`SECRET` are set; otherwise `streamlink --json` per channel (4 at a time). Current setup uses the streamlink fallback.
   - YouTube: zero-quota scrape of `youtube.com/<channel>/live` for `"isLive":true` (optional `yt-dlp` confirmation, off by default), staggered 0.75s.
   - Writes `streamers.is_live` / `last_checked_at`; offline→live starts a recorder; live-but-not-recording restarts one; live→offline calls `recorder.request_stop` (does not block the poll).
   - `TwitchEventSubClient` starts when Twitch keys exist but is a no-op (no subscriptions, no callbacks). Polling is the only real trigger.
2. **Record** (`services/recorder.py`). `streamlink --twitch-disable-ads --stdout <url> best` (or `yt-dlp` fallback for YouTube) piped into `ffmpeg -c copy -f segment -segment_time 3600` (Matroska). No re-encode, so quality is the platform's best (usually 1080p60, ~3–3.5 GB/hour). Each closed segment is remuxed to MP4 and inserted as `recording_chunks` (`queued`). On stream end: a full tail becomes a chunk; a stream with no full hour is discarded (`discarded_short`); otherwise the tail is losslessly concatenated onto chunk N as `<N>_merged.mp4` (`merged_from_chunk_id`).
3. **Process** (`services/processing_queue.py`, `neon_client.py`, `engines/neonclipping/pipeline_runner.py`). Serialized by an asyncio queue + GPU lock; stale `processing` rows return to `queued` on startup. The engine runs as a killable subprocess (`Popen`, process-tree kill on cancel or timeout, `PYTHONIOENCODING=utf-8`):
   - rank moments (motion + audio), transcribe candidates with Whisper `base`, score with the LLM, drop candidates overlapping excluded intervals by >35% (merged chunks exclude chunk N's existing clips);
   - YuNet facecam detection (`stack`, falling back to `blur`);
   - re-transcribe the top 3 with CUDA `large-v3-turbo`, render 1080×1920 with NVENC.
   Then per clip: move to `drafts\`, JPEG thumbnail, copy from `copywriter.py` (ModelArk `deepseek-v4-1-flash-261017`; `/api/coding/v3` is blocked), resumable Drive upload to `ClipPipeline/<streamer>/<date>` shared anyone-with-link (non-fatal on failure), insert `clip_drafts`. On failure the chunk is `failed` with `last_error`, and its `delete_after` is pushed to failure time + 2h so it can be retried.
4. **Approve** (`services/telegram_bot.py`). One HTML message per chunk to each allowed chat (deny-by-default `TELEGRAM_ALLOWED_CHAT_IDS`), callbacks `ap:/rj:/aa:/ra:<id>[:privacy]`. Approve answers at once ("Publishing N clip(s)…"), marks clips `[publishing…]`, and runs the upload as a background job (one job at a time, repeat taps ignored), editing the message after each clip and sending a reply if a publish fails. Reject marks `kept_local`. Shutdown mid-upload marks the clip and publication `failed` ("Interrupted by backend shutdown") for re-approval.
5. **Publish** (`services/youtube_publisher.py`). Resolves enabled `streamer_targets` (Drive-only accounts excluded), YouTube resumable upload in 8 MB pieces with 429/5xx retry, one `publications` row per channel; clip becomes `posted` if any upload succeeded. The dashboard's Post button (`POST /api/clips/{id}/approve`) does the same inline.
6. **Metrics** (`services/metrics_collector.py`, `metrics_dashboard.py`). Every 30 min: YouTube Data `videos.list` statistics plus Analytics `reports.query` (48–72h lag) into `metric_snapshots`; `GET /api/metrics/dashboard` aggregates KPIs, time series, cohorts and funnel.
7. **Retention** (`services/retention_gc.py`). Deletes a raw chunk once `delete_after` (chunk start + 2h) has passed, its status is `processed`, `failed` or `failed_permanent`, and no pending merged child depends on it.

### 2.3 Data model (SQLite, idempotent migrations in `app/db.py`)

`streamers`, `youtube_accounts` (Fernet-encrypted tokens; `oauth_provider` = `google` for YouTube channels, `google_drive` for the Drive account, each refreshed with its own OAuth client), `streamer_targets`, `stream_sessions`, `recording_chunks`, `clip_drafts`, `publications`, `metric_snapshots`, `oauth_states`.

- Chunk statuses: `queued → processing → processed | failed → deleted`; `discarded_short`.
- Clip statuses: `draft → notified → approved → posted | failed`; `kept_local` on reject.

### 2.4 API surface

`/api/health`, `/api/auth/google/*`, `/api/auth/google-drive/*`, `/api/accounts`, `/api/streamers` (+ `/{id}/targets`, `/{id}/check`, `/{id}/stop`), `/api/clips` (+ video, thumbnail, upload-drive, approve, reject, publish, publications, delete), `/api/metrics/{summary,dashboard,sync}`, `/api/system/recordings`, `/api/system/gc/run`, `/api/system/queue` (+ pause, resume, process/{id}). No authentication; CORS limited to `localhost:3000` / `127.0.0.1:3000`.

### 2.5 Frontend (Dusk redesign, see § 4.4)

| Route | Content |
| --- | --- |
| `/` Dashboard | Glass toolbar (time window, streamer, channel, Sync metrics); **Live now** list (signature); one ruled tally of six figures; best clip this week; growth chart; views by clip length; draft-to-posted funnel; by-streamer table; sortable Published Shorts table |
| `/streamers` | Channel tally, add-streamer form, watched channels table (auto-watch switch, Check now, Channels dialog for YouTube targets, ⋯ menu: Stop recording, Remove) |
| `/clips` | Status and streamer filters, drafts table (thumbnail preview, visibility, Post, Reject, ⋯ menu: Preview, Edit, Upload to Drive, Delete with confirmation) |
| `/accounts` | Connect YouTube / Connect Drive, connection tally, accounts table |
| `/system` | Pipeline tally, services health list, processing queue (pause/resume, retry), recording now, recent recordings (Clean up now) |
| `/metrics` | Redirects to `/` |

Shared building blocks live in `src/components/page-kit.tsx` (PageHeader, Panel, Tally, Ticker, StatusWord, DotTabs, Switch, Initial, Notice, EmptyState). Live data on the dashboard (`LiveNowCard`) refreshes every 30s.

---

## 3. Environment & local quirks

1. **Use the Python 3.12 venvs**, never system Python 3.14 (no `faster-whisper`/`ctranslate2`/`av` wheels): `backend\.venv\Scripts\python.exe`, `engines\neonclipping\.venv\Scripts\python.exe`.
2. **NVENC/FFmpeg mismatch:** system ffmpeg 9.0.2 needs NVENC API 13.1 (driver ≥ 610); the RTX 4060 Laptop driver 581.42 has 13.0. `pipeline_runner.py`, `setup_neonclipping.ps1` and `smoke_test_neon.py` prepend `static_ffmpeg` 8.0.1 to `PATH`.
3. **Twitch:** no Twitch keys set, so the `streamlink --json` fallback is in use (verified live against `twitch.tv/lacy`).
4. **Google OAuth:** YouTube and Drive use separate GCP clients. Keep both consent screens **In Production** so refresh tokens don't expire after 7 days. Drive redirect URI: `http://127.0.0.1:8000/api/auth/google-drive/callback`.
5. **Windows subprocesses:** `services/subprocess_compat.py` falls back to `Popen` when the event loop can't spawn subprocesses; the recorder and watchers use it.
6. **Reduced Motion** is on in this Windows account, which stills the UI's animations. Turn on Settings → Accessibility → Visual effects → Animation effects to see them.
7. **Stopping the backend:** Ctrl+C shuts down cleanly (finishes and queues the hour being recorded). Closing the terminal or killing it skips finalization: the partial `.mkv` stays orphaned in `raw recordings\`, the session stays `recording`, and a running engine subprocess may be left on the GPU. Nothing records while the backend is off.

### Environment variables (`.env`, template `.env.example`; no values here)

| Variable | Status |
| --- | --- |
| `LLM_API_KEY`, `NEONCLIPPING_LLM_KEY`, `LLM_MODEL` | Set (ModelArk standard endpoint) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Set (YouTube channels) |
| `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET` | Set; Drive account connected and active |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | Set |
| `TOKEN_ENCRYPTION_KEY` | Set (Fernet) |
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | Blank (optional; streamlink fallback) |

---

## 4. Completed work

### 4.1 Phases 1–5 (summary)
- **Phase 1:** storage layout, config, SQLite WAL schema, FastAPI app, NeonClipping engine install + `pipeline_runner.py` + smoke test (CUDA, NVENC, streamlink, Whisper `base`/`large-v3-turbo` all verified), Next.js scaffold.
- **Phase 2:** Google OAuth (YouTube), Twitch/YouTube watchers, concurrent 60-minute recorder with tail merge, retention GC.
- **Phase 3:** copywriter, serialized processing queue, dedicated 5 TB Google Drive OAuth and uploads.
- **Phase 4:** Telegram approval bot, YouTube publisher, clip approve/reject/publish endpoints and UI.
- **Phase 5:** metrics collector (Data + Analytics APIs), dashboard aggregation endpoint, metrics dashboard UI.
- **Security audit stage 1** (`C:\Users\wirya\security-audit-skill\clip\run-1\`): 0 confirmed, 6 leads to validate (unauthenticated API with cross-origin simple POST, SSRF via channel URL, unread recorder pipes, orphaned engine subprocess (since fixed, § 4.2), Drive anyone-link left after clip delete, GC TOCTOU and streamer cascade).

### 4.2 Reliability fixes (2026-11-22)
- **Failed chunks are now cleaned up.** `retention_gc.py` accepts `failed`; `processing_queue.py` pushes a failed chunk's `delete_after` to failure + `chunk_retention_seconds` (or keeps a later one) so it stays retryable for 2h.
- **Going offline no longer blocks the watcher.** `RecorderManager.request_stop()` signals the stop and returns; finalization runs in the background; `is_recording()` stays true until it finishes; `stop_all()` still waits at shutdown. Wired in `main.py` and `routers/system.py`.
- **Engine subprocess is killable.** `neon_client.arefine_and_export_with_turbo` uses `Popen` + `communicate` in a thread; on cancel or timeout it kills the whole process tree (`taskkill /T /F` on Windows, `killpg` elsewhere). Also forces `PYTHONIOENCODING=utf-8` so non-ASCII titles can't crash the JSON print.
- **Telegram Approve no longer blocks the bot** (see § 2.2 step 4).
- Tests: `backend/tests/test_fixes_gc_offline_neon.py` (8 tests: GC retry window, offline non-blocking, cancel/timeout kill the real process tree, Approve replies immediately and blocks repeat taps, shutdown mid-upload marks clips failed). Existing Phase 3/4/E2E tests updated for the new flows.

### 4.3 UI rework 1 (2026-11-22)
- Overview replaced by the metrics dashboard with a **Live now** card; the old system overview moved to `/system`; `/metrics` redirects to `/`.
- All pages moved to shared card components (now `page-kit.tsx`).

### 4.4 Dusk redesign (2026-11-22, Hallmark skill, full depth, app scope)
- **Theme:** Dusk (slate paper, periwinkle `--color-signal` for done/on, apricot `--color-owed` for waiting, `--color-error` for failures; Wix Madefor Display/Text via `next/font`).
- **Surfaces:** one library photograph of fog at dusk (`frontend/public/dusk-fog.jpg`, Thomas Bresson, CC BY 3.0, credited in the sidebar) fixed behind the app; every shadcn `Card`, the sidebar and the toolbars are smoked frosted glass (`--glass`, 22px blur, lit inset edge); dialogs and menus stay solid.
- **Motion** (transform/opacity only; all stilled under `prefers-reduced-motion`): panels rise in once, staggered; table rows follow; figures count up (`Ticker`); sliding active markers in the sidebar and DotTabs (`motion` layoutId); charts draw in and cross-fade on series change; 1px press; switch thumb slides; row hover; dialogs scale in; the photo drifts once over 48s; the recording light breathes.
- **Files:** `src/app/tokens.css` and `src/app/base.css` (Hallmark scaffold), `src/app/globals.css` (stamp, shadcn variable mapping, glass and motion classes), `src/components/page-kit.tsx`, `AppShell.tsx`, all pages and metrics components, patched shadcn primitives (button, badge, card, dialog, dropdown, select, sheet, sidebar, table, tabs, input, skeleton).
- **Token renames for shadcn:** `--color-accent` → `--color-signal`, `--color-muted` → `--color-ink-3`, `--color-accent-2` → `--color-owed`. Hallmark's `[data-slot]` base rules were scoped to `[data-hm-slot]` because shadcn uses `data-slot` on every component.
- **Checks:** Hallmark `sloplint` 63 FAIL before → 0 FAIL, 0 REFLEX after (2 dismissals on record: the requested glass, shadcn slot names). `.hallmark/compare.html` has before/after screenshots (contain account emails; delete if not wanted). The fresh-context finish review was not run.

### 4.5 First live test findings and fixes (2026-11-22 evening)
- **Found in the test:** all three recorders froze (Lacy and Ludwig at the hour mark, ohnePixel after 16 minutes) because streamlink/FFmpeg output was piped back and never read; a frozen hour was only handed to the clipper after a manual Stop; Stop restarted within a minute; the auto-watch switch failed with `database is locked` (the clip queue held a write lock across copywriter and Drive uploads); stream titles were mojibake; `test_phase5_metrics.py` booted the real backend on live data during every test run.
- **Recorder** (`services/recorder.py`): logs go to `Clips_Store\logs\<prefix>_{source,ffmpeg}.log`; FFmpeg runs `-nostats -loglevel warning`; a stall watchdog restarts a recorder whose file stops growing for `recording_stall_seconds` (120); every session records `end_reason` and `last_error` (new `stream_sessions` columns); unclosed segments are recovered by remuxing (`readable_duration`) instead of being deleted as empty; a recording cut short by Stop, a stall or a crash keeps a partial of at least `min_partial_seconds` (300) and clips it; large unreadable footage is kept as `unreadable`, never deleted; `recover_interrupted()` runs at startup for sessions a hard stop left `recording`.
- **Stop holds:** Stop sets `streamers.hold_until_offline`; the watcher and Check now won't restart until the streamer is seen offline; `POST /api/streamers/{id}/resume` lifts it.
- **Alerts:** `TelegramBotService.notify_recording_problem` on the 1st, 3rd and every 10th consecutive failure per streamer.
- **Per-streamer clip settings:** `clip_opts.clip_count` (1–10, passed to the engine) and `clip_opts.hashtags` (normalized, appended under each draft's description); validated in `routers/streamers.py`.
- **Ranking learning:** `pipeline_runner.py` returns each clip's `features`, stored as `clip_drafts.features_json`; `services/learning.py` rates drafts (rejected 2, posted 4, posted top third by engaged views 5, bottom third 3) every 2 minutes through `engines/neonclipping/learn_cli.py` into `neon_tmp\weights.json`; `GET /api/system/learning`, `POST /api/system/learning/sync`. Drafts made before this change have no features and are not used.
- **API robustness:** the queue commits each draft as it is made; SQLite `busy_timeout` 15s; unhandled errors return JSON 500 with CORS headers (no more bare "Failed to fetch"); the frontend reports an unreachable backend in plain words; stream titles are repaired (`app/textfix.py`).
- **Tests:** `tests/conftest.py` sandboxes every test (temp paths, workers off, no Telegram); `tests/test_recorder_hold_learning.py` (stall, partial-on-stop with real FFmpeg, hold, learning sync, clip settings, JSON errors, title repair).
- **UI:** Streamers has clips per hour and hashtags in the add form, a Settings dialog per streamer, Stop/Resume in the ⋯ menu, and "Live, stopped by you" status; Clips has a visible Edit post button and a fuller editor (character counts, hashtags editable); System has Recording sessions and Clip ranking panels; the sidebar is flush left and centred when collapsed. Hallmark sweep: 0 FAIL.
- **Data note:** Lacy's 13:33 chunk file had disappeared from `raw recordings`; it was restored as a hard link from the engine's staged copy so the queued chunk can finish.

### 4.6 Verification baseline
- Backend: `backend\.venv\Scripts\python.exe -m pytest -q` → **71 passed** (sandboxed by `tests/conftest.py`; never touches the real Clips_Store).
- Frontend: `npx tsc --noEmit`, `npm run lint`, `npm run build` all pass.

---

## 5. Next steps

**Now:** first live test of one batch (a batch needs at least one full 60-minute chunk). Watch the backend terminal and System → Recording now.

**Done from the backlog:** recorder error reporting and the ranking learning loop (§ 4.5).

**Ideas raised, not scheduled:** vision for moment scoring (send 3 frames per candidate to a ModelArk vision model in the same judging call), per-streamer recording quality.

**Known gaps (not yet scheduled):**
- A hard kill can still leave an engine process on the GPU (no PID file yet); leftover recordings are now recovered at startup.
- Twitch EventSub is a no-op; polling means up to ~1 minute of each stream is missed.
- No per-streamer recording quality setting (`best` is hard-coded).
- `/api/health` reports NeonClipping "Offline" when its UI server (port 8770) isn't running, although the pipeline doesn't need it.
- Telegram is not notified when clipping (not recording) fails.
- Security audit leads in § 4.1 still need validation (no API auth is the main one).

---

## 6. Run commands (PowerShell)

```powershell
# Backend tests
cd c:\Users\wirya\OneDrive\Documents\clip\backend
.\.venv\Scripts\python.exe -m pytest -q

# Engine smoke test (CUDA + NVENC)
cd c:\Users\wirya\OneDrive\Documents\clip
.\engines\neonclipping\.venv\Scripts\python.exe .\scripts\smoke_test_neon.py

# Start the backend (records, clips, posts; stop with Ctrl+C)
cd c:\Users\wirya\OneDrive\Documents\clip\backend
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000

# API only, no background workers (safe for UI work)
$env:ENABLE_BACKGROUND_WORKERS = "false"; .\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000

# Frontend
cd c:\Users\wirya\OneDrive\Documents\clip\frontend
npx tsc --noEmit; npm run lint; npm run build
npm run dev
```
