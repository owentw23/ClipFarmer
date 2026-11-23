from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
import inspect
import logging
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import open_connection, utcnow_iso
from app.services.twitch_watcher import TwitchEventSubClient, TwitchWatcher
from app.services.youtube_watcher import YouTubeWatcher
from app.textfix import repair_text

logger = logging.getLogger(__name__)


@dataclass
class LiveStreamInfo:
    """Normalized live stream metadata emitted when a streamer is live."""

    streamer_id: int
    streamer_name: str
    streamer_slug: str
    platform: str
    channel_ref: str
    stream_url: str
    external_stream_id: str
    title: str


class WatcherManager:
    """Coordinates periodic & on-demand streamer live checks across Twitch and YouTube."""

    def __init__(
        self,
        settings: Settings,
        on_online: Callable[[LiveStreamInfo], Awaitable[Any] | Any] | None = None,
        on_offline: Callable[[int], Awaitable[Any] | Any] | None = None,
        twitch_watcher: TwitchWatcher | None = None,
        youtube_watcher: YouTubeWatcher | None = None,
        youtube_stagger_sec: float = 0.75,
        is_recording: Callable[[int], bool] | None = None,
        offline_confirm_polls: int = 1,
    ) -> None:
        self.settings = settings
        self.on_online = on_online
        self.on_offline = on_offline
        self.is_recording = is_recording
        self.offline_confirm_polls = max(1, int(offline_confirm_polls))
        self._offline_strikes: dict[int, int] = {}
        self.twitch_watcher = twitch_watcher or TwitchWatcher(
            client_id=getattr(settings, "twitch_client_id", ""),
            client_secret=getattr(settings, "twitch_client_secret", ""),
        )
        self.youtube_watcher = youtube_watcher or YouTubeWatcher(
            cookies_file=getattr(settings, "youtube_cookies_file", ""),
        )
        self.youtube_stagger_sec = max(0.0, float(youtube_stagger_sec))
        self.eventsub_client = TwitchEventSubClient(
            client_id=getattr(settings, "twitch_client_id", ""),
            client_secret=getattr(settings, "twitch_client_secret", ""),
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _needs_recording(self, streamer_id: int) -> bool:
        return self.is_recording is not None and not self.is_recording(streamer_id)

    async def _invoke_callback(self, fn: Callable[..., Any] | None, arg: Any) -> None:
        if fn is None:
            return
        result = fn(arg)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _build_live_info(row: Any, stream_data: dict[str, Any]) -> LiveStreamInfo:
        return LiveStreamInfo(
            streamer_id=int(row["id"]),
            streamer_name=str(row["name"]),
            streamer_slug=str(row["slug"]),
            platform=str(row["platform"]),
            channel_ref=str(row["channel_ref"]),
            stream_url=str(stream_data.get("stream_url") or row["channel_url"]),
            external_stream_id=str(stream_data.get("external_stream_id") or ""),
            title=repair_text(str(stream_data.get("title") or "")),
        )

    @staticmethod
    def _held(row: Any) -> bool:
        """Recording was stopped by hand: stay off until this stream ends."""
        return "hold_until_offline" in row.keys() and bool(row["hold_until_offline"])

    async def _check_youtube_row(self, row: Any) -> dict[str, Any] | None:
        return await self.youtube_watcher.check_channel(str(row["channel_ref"]))

    async def poll_once(self, db_path: str | Path | None = None) -> list[LiveStreamInfo]:
        """Poll all auto_watch=1 streamers, update DB live state, and fire transition callbacks."""
        conn = await open_connection(db_path or self.settings.db_path)
        transitions_online: list[LiveStreamInfo] = []
        transitions_offline: list[int] = []
        live_streams: list[LiveStreamInfo] = []

        try:
            async with conn.execute(
                "SELECT * FROM streamers WHERE auto_watch = 1 ORDER BY id ASC"
            ) as cur:
                rows = await cur.fetchall()

            if not rows:
                return []

            twitch_rows = [r for r in rows if r["platform"] == "twitch"]
            youtube_rows = [r for r in rows if r["platform"] == "youtube"]

            twitch_results: dict[str, dict[str, Any]] = {}
            twitch_skipped_recording: set[int] = set()
            twitch_errors: set[str] = set()
            if twitch_rows:
                # Without Twitch API creds, streamlink --json spawns a subprocess per channel.
                # Channels already actively recording do not need redundant probing: the
                # recorder's own streamlink exits when the stream actually ends.
                use_fallback = (
                    isinstance(self.twitch_watcher, TwitchWatcher)
                    and not self.twitch_watcher.has_credentials
                )
                logins: list[str] = []
                for r in twitch_rows:
                    sid = int(r["id"])
                    if use_fallback and self.is_recording is not None and self.is_recording(sid):
                        twitch_skipped_recording.add(sid)
                    else:
                        logins.append(str(r["channel_ref"]))
                if logins:
                    twitch_results = await self.twitch_watcher.check_channels(logins)
                    twitch_errors = set(getattr(self.twitch_watcher, "last_probe_errors", ()))

            youtube_results: dict[int, dict[str, Any] | None] = {}
            youtube_errors: set[int] = set()
            for idx, yt_row in enumerate(youtube_rows):
                if idx > 0 and self.youtube_stagger_sec > 0:
                    await asyncio.sleep(self.youtube_stagger_sec)
                yt_id = int(yt_row["id"])
                yt_ref = str(yt_row["channel_ref"])
                youtube_results[yt_id] = await self._check_youtube_row(yt_row)
                if yt_ref in getattr(self.youtube_watcher, "last_probe_errors", ()):
                    youtube_errors.add(yt_id)

            checked_at = utcnow_iso()
            for row in rows:
                sid = int(row["id"])
                platform = str(row["platform"])
                if sid in twitch_skipped_recording:
                    self._offline_strikes.pop(sid, None)
                    await conn.execute(
                        "UPDATE streamers SET last_checked_at = ?, updated_at = ? WHERE id = ?",
                        (checked_at, checked_at, sid),
                    )
                    continue

                probe_failed = False
                if platform == "twitch":
                    key = str(row["channel_ref"]).strip().lstrip("@").lower()
                    stream_data = twitch_results.get(key)
                    probe_failed = key in twitch_errors
                else:
                    stream_data = youtube_results.get(sid)
                    probe_failed = sid in youtube_errors

                if probe_failed:
                    # Indeterminate probe (timeout / network error): keep prior live state
                    # and never kill an active recording over a probe timeout.
                    await conn.execute(
                        "UPDATE streamers SET last_checked_at = ?, updated_at = ? WHERE id = ?",
                        (checked_at, checked_at, sid),
                    )
                    continue

                was_live = bool(row["is_live"])
                is_live = stream_data is not None

                if is_live:
                    self._offline_strikes.pop(sid, None)
                elif self.is_recording is not None and self.is_recording(sid):
                    strikes = self._offline_strikes.get(sid, 0) + 1
                    self._offline_strikes[sid] = strikes
                    if strikes < self.offline_confirm_polls:
                        await conn.execute(
                            "UPDATE streamers SET last_checked_at = ?, updated_at = ? WHERE id = ?",
                            (checked_at, checked_at, sid),
                        )
                        continue
                    self._offline_strikes.pop(sid, None)
                else:
                    self._offline_strikes.pop(sid, None)

                held = self._held(row)
                await conn.execute(
                    """
                    UPDATE streamers
                    SET is_live = ?, last_checked_at = ?, updated_at = ?,
                        hold_until_offline = CASE WHEN ? THEN hold_until_offline ELSE 0 END
                    WHERE id = ?
                    """,
                    (1 if is_live else 0, checked_at, checked_at, 1 if is_live else 0, sid),
                )

                if is_live and stream_data is not None:
                    info = self._build_live_info(row, stream_data)
                    live_streams.append(info)
                    if not held and (not was_live or self._needs_recording(sid)):
                        transitions_online.append(info)
                elif was_live and not is_live:
                    transitions_offline.append(sid)

            await conn.commit()
        finally:
            await conn.close()

        for info in transitions_online:
            await self._invoke_callback(self.on_online, info)
        for sid in transitions_offline:
            await self._invoke_callback(self.on_offline, sid)

        return live_streams

    async def check_streamer(
        self,
        streamer_id: int,
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Immediately check one streamer by ID, update DB, and invoke transition callbacks."""
        conn = await open_connection(db_path or self.settings.db_path)
        info: LiveStreamInfo | None = None
        was_live = False
        is_live = False
        checked_at = utcnow_iso()

        try:
            async with conn.execute(
                "SELECT * FROM streamers WHERE id = ?",
                (streamer_id,),
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                raise LookupError(f"Streamer {streamer_id} not found")

            was_live = bool(row["is_live"])
            platform = str(row["platform"])
            stream_data: dict[str, Any] | None = None

            if platform == "twitch":
                key = str(row["channel_ref"]).strip().lstrip("@").lower()
                results = await self.twitch_watcher.check_channels([key])
                stream_data = results.get(key)
            elif platform == "youtube":
                stream_data = await self._check_youtube_row(row)
            else:
                raise ValueError(f"Unsupported platform '{platform}' for streamer {streamer_id}")

            is_live = stream_data is not None
            checked_at = utcnow_iso()
            held = self._held(row) and is_live

            await conn.execute(
                """
                UPDATE streamers
                SET is_live = ?, last_checked_at = ?, updated_at = ?,
                    hold_until_offline = CASE WHEN ? THEN hold_until_offline ELSE 0 END
                WHERE id = ?
                """,
                (1 if is_live else 0, checked_at, checked_at, 1 if is_live else 0, streamer_id),
            )
            await conn.commit()

            if is_live and stream_data is not None:
                info = self._build_live_info(row, stream_data)
        finally:
            await conn.close()

        if is_live and info is not None and not held and (not was_live or self._needs_recording(streamer_id)):
            await self._invoke_callback(self.on_online, info)
        elif was_live and not is_live:
            await self._invoke_callback(self.on_offline, streamer_id)

        return {
            "streamer_id": streamer_id,
            "is_live": is_live,
            "stream": asdict(info) if info is not None else None,
            "checked_at": checked_at,
            "held": held,
        }

    async def _poll_loop(self) -> None:
        interval = max(1.0, float(getattr(self.settings, "watcher_poll_interval_sec", 60)))
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Watcher poll_once error: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        """Start the background watcher polling loop and optional EventSub client."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        await self.eventsub_client.start()
        self._task = asyncio.create_task(self._poll_loop(), name="watcher-manager")

    async def stop(self) -> None:
        """Stop the background watcher polling loop and EventSub client cleanly."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.eventsub_client.stop()
