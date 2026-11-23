from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import TYPE_CHECKING, Any

import aiosqlite
import httpx

from app.config import Settings
from app.db import open_connection, utcnow_iso

if TYPE_CHECKING:
    from app.services.youtube_publisher import YouTubePublisherService

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_MESSAGE_CHARS = 4096
VALID_PRIVACY_STATUSES = frozenset({"public", "unlisted", "private"})
_CHAT_ID_RE = re.compile(r"^-?\d+$")
_CALLBACK_RE = re.compile(r"^(ap|rj|aa|ra):(\d+)(?::([a-z]+))?$")
_TOKEN_IN_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


class TelegramBotError(RuntimeError):
    """Raised when Telegram Bot API operations fail."""


def _scrub_token(text: str, token: str = "") -> str:
    cleaned = str(text or "")
    if token:
        cleaned = cleaned.replace(token, "<redacted>")
    return _TOKEN_IN_URL_RE.sub("bot<redacted>", cleaned)


def parse_allowed_chat_ids(raw: str) -> set[str]:
    """Parse comma/whitespace-separated integer chat IDs into a normalized string set."""
    if not raw:
        return set()
    allowed: set[str] = set()
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        if _CHAT_ID_RE.fullmatch(part):
            allowed.add(str(int(part)))
    return allowed


def _format_mmss(seconds: float | int | None) -> str:
    if seconds is None:
        return "00:00"
    total = max(0, int(round(float(seconds))))
    return f"{total // 60:02d}:{total % 60:02d}"


def _clip_text(value: str, max_len: int) -> str:
    text = re.sub(r"\s+", " ", (value or "")).strip()
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return f"{text[: max_len - 3].rstrip()}..."


class TelegramBotService:
    """Async Telegram Bot notifier and inline-keyboard approval poller."""

    def __init__(
        self,
        settings: Settings,
        publisher: YouTubePublisherService | None = None,
        http_client: httpx.AsyncClient | None = None,
        *,
        poll_timeout_sec: int = 25,
    ) -> None:
        self.settings = settings
        self._publisher = publisher
        self._http_client = http_client
        self.poll_timeout_sec = max(1, int(poll_timeout_sec))
        self._offset: int | None = None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Background YouTube publish jobs started from Approve buttons.
        self._publish_tasks: set[asyncio.Task[dict[str, Any]]] = set()
        self._publish_lock = asyncio.Lock()
        self._in_flight: set[int] = set()

    @property
    def publisher(self) -> YouTubePublisherService:
        if self._publisher is None:
            from app.services.youtube_publisher import YouTubePublisherService

            self._publisher = YouTubePublisherService(
                self.settings,
                http_client=self._http_client,
            )
        return self._publisher

    def _bot_token(self) -> str:
        return (self.settings.telegram_bot_token or "").strip()

    def _allowed_chat_ids(self) -> set[str]:
        return parse_allowed_chat_ids(self.settings.telegram_allowed_chat_ids or "")

    def is_configured(self) -> bool:
        """Return True only when both bot token and at least one allowed chat ID exist."""
        return bool(self._bot_token() and self._allowed_chat_ids())

    def is_authorized_chat(self, chat_id: int | str | None) -> bool:
        """Strict deny-by-default authorization check against configured chat IDs."""
        if chat_id is None or isinstance(chat_id, bool):
            return False
        raw = str(chat_id).strip()
        if not _CHAT_ID_RE.fullmatch(raw):
            return False
        allowed = self._allowed_chat_ids()
        if not allowed:
            return False
        return str(int(raw)) in allowed

    async def _api_call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        token = self._bot_token()
        if not token:
            raise TelegramBotError("Telegram bot token is not configured")
        url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
        req_timeout = float(timeout if timeout is not None else max(15, self.poll_timeout_sec + 10))

        try:
            if self._http_client is not None:
                resp = await self._http_client.post(url, json=payload or {}, timeout=req_timeout)
            else:
                async with httpx.AsyncClient(timeout=req_timeout) as client:
                    resp = await client.post(url, json=payload or {})
        except httpx.HTTPError as exc:
            safe_detail = _scrub_token(str(exc), token)
            raise TelegramBotError(
                f"Telegram API {method} network error ({type(exc).__name__}): {safe_detail}"
            ) from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise TelegramBotError(
                f"Telegram API {method} returned non-JSON HTTP {resp.status_code}"
            ) from exc

        if resp.status_code >= 400 or not body.get("ok"):
            desc = _scrub_token(str(body.get("description") or resp.text or ""), token)
            raise TelegramBotError(
                f"Telegram API {method} failed (HTTP {resp.status_code}): {desc}"
            )
        return body.get("result")

    async def _answer_callback(
        self,
        callback_id: str | None,
        text: str,
        *,
        show_alert: bool = False,
    ) -> None:
        if not callback_id:
            return
        try:
            await self._api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": text[:200],
                    "show_alert": show_alert,
                },
                timeout=10.0,
            )
        except TelegramBotError as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def _render_chunk_message(
        self,
        chunk_ctx: dict[str, Any],
        clips: list[dict[str, Any]],
        targets: list[dict[str, Any]],
        publications_by_clip: dict[int, list[dict[str, Any]]],
        *,
        title_cap: int,
        desc_cap: int,
    ) -> str:
        streamer_name = html.escape(str(chunk_ctx.get("streamer_name") or "Unknown"), quote=True)
        platform = html.escape(str(chunk_ctx.get("platform") or "stream"), quote=True)
        stream_title = html.escape(
            _clip_text(str(chunk_ctx.get("stream_title") or ""), 120), quote=True
        )
        chunk_index = int(chunk_ctx.get("chunk_index") or 1)
        started_at = html.escape(str(chunk_ctx.get("started_at") or "")[:19], quote=True)
        dur_sec = chunk_ctx.get("duration_sec")

        target_names = [
            html.escape(str(t.get("channel_title") or t.get("channel_id") or "YouTube"), quote=True)
            for t in targets
        ]
        targets_line = ", ".join(target_names) if target_names else "None configured"

        lines: list[str] = [f"<b>{streamer_name}</b> ({platform}) — Chunk #{chunk_index}"]
        if stream_title:
            lines.append(f"Stream: {stream_title}")
        window_parts: list[str] = []
        if started_at:
            window_parts.append(started_at)
        if dur_sec is not None:
            window_parts.append(_format_mmss(float(dur_sec)))
        if window_parts:
            lines.append(f"Window: {' · '.join(window_parts)}")
        lines.append(f"Targets: {targets_line}")

        for idx, clip in enumerate(clips, start=1):
            num = int(clip.get("draft_number") or idx)
            raw_status = str(clip.get("status") or "draft")
            if int(clip["id"]) in self._in_flight:
                raw_status = "publishing…"
            status = html.escape(raw_status, quote=True)
            title = html.escape(
                _clip_text(str(clip.get("title") or f"Clip {num}"), title_cap),
                quote=True,
            )
            desc = html.escape(
                _clip_text(str(clip.get("description") or ""), desc_cap),
                quote=True,
            )
            start_str = _format_mmss(clip.get("start_sec"))
            end_str = _format_mmss(clip.get("end_sec"))

            lines.append("")
            lines.append(f"<b>#{num}</b> [{status}] {title}")
            lines.append(f"Time: {start_str}-{end_str}")
            if desc:
                lines.append(desc)

            drive_url = str(clip.get("drive_url") or "").strip()
            if drive_url:
                safe_drive = html.escape(drive_url, quote=True)
                lines.append(f'Drive: <a href="{safe_drive}">Open MP4</a>')
            else:
                lines.append("Drive: Not uploaded")

            pubs = publications_by_clip.get(int(clip["id"]), [])
            yt_links: list[str] = []
            yt_errors: list[str] = []
            for pub in pubs:
                ch_label = html.escape(
                    str(pub.get("channel_title") or pub.get("channel_id") or "YouTube"),
                    quote=True,
                )
                yt_url = str(pub.get("youtube_url") or "").strip()
                if pub.get("status") == "published" and yt_url:
                    safe_yt = html.escape(yt_url, quote=True)
                    yt_links.append(f'<a href="{safe_yt}">{ch_label}</a>')
                elif pub.get("status") == "failed":
                    yt_errors.append(ch_label)
            if yt_links:
                lines.append(f"YouTube: {' | '.join(yt_links)}")
            elif yt_errors:
                lines.append(f"YouTube Failed: {', '.join(yt_errors)}")

        return "\n".join(lines)

    def build_chunk_summary_payload(
        self,
        chunk_ctx: dict[str, Any],
        clips: list[dict[str, Any]],
        targets: list[dict[str, Any]],
        publications_by_clip: dict[int, list[dict[str, Any]]],
    ) -> tuple[str, dict[str, Any]]:
        """Build HTML summary text (<= 4096 chars) and inline keyboard markup for a chunk."""
        text = self._render_chunk_message(
            chunk_ctx,
            clips,
            targets,
            publications_by_clip,
            title_cap=100,
            desc_cap=240,
        )
        for title_cap, desc_cap in ((80, 140), (60, 80), (45, 0)):
            if len(text) <= TELEGRAM_MAX_MESSAGE_CHARS:
                break
            text = self._render_chunk_message(
                chunk_ctx,
                clips,
                targets,
                publications_by_clip,
                title_cap=title_cap,
                desc_cap=desc_cap,
            )

        if len(text) > TELEGRAM_MAX_MESSAGE_CHARS:
            # Strip HTML tags before hard truncation so we never emit broken tags.
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            text = html.escape(
                f"{plain[: TELEGRAM_MAX_MESSAGE_CHARS - 3]}...",
                quote=True,
            )

        keyboard: list[list[dict[str, str]]] = []
        actionable_count = 0
        for idx, clip in enumerate(clips, start=1):
            cid = int(clip["id"])
            num = int(clip.get("draft_number") or idx)
            if str(clip.get("status") or "") not in {"posted", "deleted"}:
                actionable_count += 1
            ap_data = f"ap:{cid}"
            rj_data = f"rj:{cid}"
            if len(ap_data.encode("utf-8")) >= 64 or len(rj_data.encode("utf-8")) >= 64:
                raise TelegramBotError(f"Callback data exceeded 64 bytes for clip {cid}")
            keyboard.append(
                [
                    {"text": f"Approve #{num}", "callback_data": ap_data},
                    {"text": f"Reject #{num}", "callback_data": rj_data},
                ]
            )

        chunk_id = int(chunk_ctx.get("chunk_id") or 0)
        if chunk_id > 0 and (len(clips) > 1 or actionable_count > 0):
            aa_data = f"aa:{chunk_id}"
            ra_data = f"ra:{chunk_id}"
            if len(aa_data.encode("utf-8")) >= 64 or len(ra_data.encode("utf-8")) >= 64:
                raise TelegramBotError(f"Callback data exceeded 64 bytes for chunk {chunk_id}")
            keyboard.append(
                [
                    {"text": "Approve All", "callback_data": aa_data},
                    {"text": "Reject All", "callback_data": ra_data},
                ]
            )

        return text, {"inline_keyboard": keyboard}

    async def _load_chunk_bundle(
        self,
        db: aiosqlite.Connection,
        chunk_id: int,
        clip_ids: list[int] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
        async with db.execute(
            """
            SELECT rc.id AS chunk_id,
                   rc.chunk_index,
                   rc.started_at,
                   rc.duration_sec,
                   rc.streamer_id,
                   s.name AS streamer_name,
                   s.platform AS platform,
                   ss.title AS stream_title
            FROM recording_chunks rc
            JOIN streamers s ON s.id = rc.streamer_id
            LEFT JOIN stream_sessions ss ON ss.id = rc.session_id
            WHERE rc.id = ?
            """,
            (chunk_id,),
        ) as cur:
            chunk_row = await cur.fetchone()
        if chunk_row is None:
            return None, [], [], {}
        chunk_ctx = dict(chunk_row)

        if clip_ids:
            placeholders = ", ".join("?" for _ in clip_ids)
            clip_sql = (
                f"SELECT * FROM clip_drafts WHERE chunk_id = ? AND id IN ({placeholders}) "
                "ORDER BY draft_number ASC, id ASC"
            )
            clip_params: list[Any] = [chunk_id, *[int(cid) for cid in clip_ids]]
        else:
            clip_sql = (
                "SELECT * FROM clip_drafts WHERE chunk_id = ? ORDER BY draft_number ASC, id ASC"
            )
            clip_params = [chunk_id]

        async with db.execute(clip_sql, clip_params) as cur:
            clips = [dict(row) for row in await cur.fetchall()]

        async with db.execute(
            """
            SELECT ya.id AS youtube_account_id,
                   ya.channel_id,
                   ya.channel_title,
                   st.default_privacy
            FROM streamer_targets st
            JOIN youtube_accounts ya ON ya.id = st.youtube_account_id
            WHERE st.streamer_id = ?
              AND st.enabled = 1
              AND ya.status = 'active'
              AND COALESCE(ya.oauth_provider, 'google') != 'google_drive'
              AND ya.channel_id NOT LIKE 'drive-only:%'
            ORDER BY st.id ASC
            """,
            (int(chunk_ctx["streamer_id"]),),
        ) as cur:
            targets = [dict(row) for row in await cur.fetchall()]

        pubs_by_clip: dict[int, list[dict[str, Any]]] = {}
        if clips:
            cids = [int(c["id"]) for c in clips]
            placeholders = ", ".join("?" for _ in cids)
            async with db.execute(
                f"""
                SELECT p.*, ya.channel_id, ya.channel_title
                FROM publications p
                LEFT JOIN youtube_accounts ya ON ya.id = p.youtube_account_id
                WHERE p.clip_draft_id IN ({placeholders})
                ORDER BY p.id DESC
                """,
                cids,
            ) as cur:
                for row in await cur.fetchall():
                    item = dict(row)
                    pubs_by_clip.setdefault(int(item["clip_draft_id"]), []).append(item)

        return chunk_ctx, clips, targets, pubs_by_clip

    async def notify_chunk_ready(
        self,
        chunk_id: int,
        clip_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Send one combined Telegram summary message with approval buttons for a processed chunk."""
        if not self.is_configured() or not clip_ids:
            return []

        conn = await open_connection(self.settings.db_path)
        try:
            chunk_ctx, clips, targets, pubs_by_clip = await self._load_chunk_bundle(
                conn, int(chunk_id), clip_ids=clip_ids
            )
            if chunk_ctx is None or not clips:
                return []

            text, reply_markup = self.build_chunk_summary_payload(
                chunk_ctx, clips, targets, pubs_by_clip
            )

            sent_messages: list[dict[str, Any]] = []
            for chat_id in sorted(self._allowed_chat_ids()):
                try:
                    msg = await self._api_call(
                        "sendMessage",
                        {
                            "chat_id": int(chat_id),
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "reply_markup": reply_markup,
                        },
                        timeout=15.0,
                    )
                    if isinstance(msg, dict):
                        sent_messages.append(msg)
                except TelegramBotError as exc:
                    logger.warning(
                        "Failed sending Telegram notification to chat %s for chunk %s: %s",
                        chat_id,
                        chunk_id,
                        exc,
                    )

            if sent_messages:
                first_msg = sent_messages[0]
                tg_chat_id = str(
                    (first_msg.get("chat") or {}).get("id")
                    or sorted(self._allowed_chat_ids())[0]
                )
                tg_message_id = first_msg.get("message_id")
                cids = [int(c["id"]) for c in clips]
                placeholders = ", ".join("?" for _ in cids)
                now = utcnow_iso()
                await conn.execute(
                    f"""
                    UPDATE clip_drafts
                    SET tg_chat_id = ?,
                        tg_message_id = ?,
                        status = CASE WHEN status = 'draft' THEN 'notified' ELSE status END,
                        updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [tg_chat_id, tg_message_id, now, *cids],
                )
                await conn.commit()

            return sent_messages
        finally:
            await conn.close()

    async def notify_recording_problem(self, problem: dict[str, Any]) -> list[dict[str, Any]]:
        """Tell the allowed chats a recorder stalled or failed.

        Sent on the first failure in a row, again at the third and every tenth, so a
        recorder that keeps failing every minute cannot flood the chat.
        """
        if not self.is_configured():
            return []
        failures = int(problem.get("failures") or 1)
        if failures not in (1, 3) and failures % 10 != 0:
            return []
        name = html.escape(str(problem.get("streamer_name") or "A streamer"), quote=True)
        reason = "Recording stalled" if problem.get("end_reason") == "stalled" else "Recorder failed"
        error = html.escape(_clip_text(str(problem.get("error") or ""), 700), quote=True)
        saved = int(problem.get("chunks_saved") or 0)
        lines = [f"<b>{reason}: {name}</b>", error]
        lines.append(
            f"Saved and queued {saved} chunk(s) from this session." if saved else "Nothing from this session was saved."
        )
        if failures > 1:
            lines.append(f"Failed {failures} times in a row. It retries every minute while the stream is live.")
        else:
            lines.append("It retries automatically within a minute while the stream is live.")
        sent: list[dict[str, Any]] = []
        for chat_id in sorted(self._allowed_chat_ids()):
            try:
                msg = await self._api_call(
                    "sendMessage",
                    {"chat_id": int(chat_id), "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=15.0,
                )
                if isinstance(msg, dict):
                    sent.append(msg)
            except TelegramBotError as exc:
                logger.warning("Failed sending recording alert to chat %s: %s", chat_id, exc)
        return sent

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        """Process one Telegram callback_query update with strict chat authorization."""
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return {"ok": True, "ignored": True}

        callback_id = callback.get("id")
        data = str(callback.get("data") or "").strip()
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")

        if not self.is_authorized_chat(chat_id):
            await self._answer_callback(callback_id, "Unauthorized chat", show_alert=True)
            return {"ok": False, "reason": "unauthorized"}

        match = _CALLBACK_RE.fullmatch(data)
        if not match:
            await self._answer_callback(callback_id, "Unsupported action", show_alert=True)
            return {"ok": False, "reason": "invalid_callback"}

        action, target_raw, privacy = match.groups()
        target_id = int(target_raw)
        if privacy is not None:
            if action not in {"ap", "aa"} or privacy not in VALID_PRIVACY_STATUSES:
                await self._answer_callback(callback_id, "Invalid privacy setting", show_alert=True)
                return {"ok": False, "reason": "invalid_privacy"}

        conn = await open_connection(self.settings.db_path)
        try:
            chunk_id: int | None = None
            target_clips: list[dict[str, Any]] = []

            if action in {"ap", "rj"}:
                async with conn.execute(
                    "SELECT id, chunk_id, status FROM clip_drafts WHERE id = ?",
                    (target_id,),
                ) as cur:
                    row = await cur.fetchone()
                if row is None:
                    await self._answer_callback(callback_id, "Clip not found", show_alert=True)
                    return {"ok": False, "reason": "not_found"}
                target_clips = [dict(row)]
                chunk_id = int(row["chunk_id"]) if row["chunk_id"] is not None else None
            else:
                chunk_id = target_id
                async with conn.execute(
                    "SELECT id, chunk_id, status FROM clip_drafts WHERE chunk_id = ? ORDER BY draft_number ASC, id ASC",
                    (chunk_id,),
                ) as cur:
                    target_clips = [dict(r) for r in await cur.fetchall()]
                if not target_clips:
                    await self._answer_callback(callback_id, "No clips found", show_alert=True)
                    return {"ok": False, "reason": "not_found"}

            queued_count = 0
            skipped_count = 0
            rejected_count = 0
            in_flight_hit = False

            if action in {"ap", "aa"}:
                # Uploads can take minutes. Reply to Telegram now and publish in a background
                # job, so the poll loop keeps picking up other button presses meanwhile.
                to_publish: list[int] = []
                for clip in target_clips:
                    cid = int(clip["id"])
                    if cid in self._in_flight:
                        # Repeat tap while already publishing: never upload twice.
                        in_flight_hit = True
                        skipped_count += 1
                        continue
                    if str(clip.get("status") or "") in {"posted", "deleted"}:
                        skipped_count += 1
                        continue
                    to_publish.append(cid)

                if to_publish:
                    now = utcnow_iso()
                    for cid in to_publish:
                        await conn.execute(
                            "UPDATE clip_drafts SET status = 'approved', updated_at = ? WHERE id = ?",
                            (now, cid),
                        )
                    await conn.commit()
                    self._in_flight.update(to_publish)
                    task = asyncio.create_task(
                        self._publish_job(
                            to_publish,
                            privacy=privacy,
                            chunk_id=chunk_id,
                            chat_id=chat_id,
                            message_id=message_id,
                        ),
                        name=f"telegram-publish-{'-'.join(map(str, to_publish))}",
                    )
                    self._publish_tasks.add(task)
                    task.add_done_callback(self._publish_tasks.discard)
                    queued_count = len(to_publish)
                    ack_text = f"Publishing {queued_count} clip(s) to YouTube…"
                elif in_flight_hit:
                    ack_text = "Already publishing"
                else:
                    ack_text = "Already posted"
            else:
                now = utcnow_iso()
                for clip in target_clips:
                    cid = int(clip["id"])
                    if cid in self._in_flight:
                        skipped_count += 1
                        continue
                    if str(clip.get("status") or "") in {"posted", "deleted"}:
                        skipped_count += 1
                        continue
                    await conn.execute(
                        "UPDATE clip_drafts SET status = 'kept_local', updated_at = ? WHERE id = ?",
                        (now, cid),
                    )
                    rejected_count += 1
                await conn.commit()
                ack_text = (
                    f"Marked {rejected_count} clip(s) as kept_local"
                    if rejected_count > 0
                    else "Already finalized"
                )

            await self._answer_callback(callback_id, ack_text)
            await self._refresh_chunk_message(conn, chunk_id, chat_id, message_id)

            return {
                "ok": True,
                "action": action,
                "target_id": target_id,
                "queued_count": queued_count,
                "rejected_count": rejected_count,
                "skipped_count": skipped_count,
            }
        finally:
            await conn.close()

    async def _refresh_chunk_message(
        self,
        conn: aiosqlite.Connection,
        chunk_id: int | None,
        chat_id: int | str | None,
        message_id: int | None,
    ) -> None:
        """Re-render a chunk's summary message in place from current DB state."""
        if chunk_id is None or chat_id is None or message_id is None:
            return
        chunk_ctx, all_clips, targets, pubs_by_clip = await self._load_chunk_bundle(conn, chunk_id)
        if chunk_ctx is None or not all_clips:
            return
        new_text, new_markup = self.build_chunk_summary_payload(
            chunk_ctx, all_clips, targets, pubs_by_clip
        )
        try:
            await self._api_call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": new_text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "reply_markup": new_markup,
                },
                timeout=15.0,
            )
        except TelegramBotError as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("editMessageText failed for chunk %s: %s", chunk_id, exc)

    async def _publish_job(
        self,
        clip_ids: list[int],
        *,
        privacy: str | None,
        chunk_id: int | None,
        chat_id: int | str | None,
        message_id: int | None,
    ) -> dict[str, Any]:
        """Publish approved clips in the background, editing the Telegram message per clip."""
        remaining = list(clip_ids)
        posted_count = 0
        failed_count = 0
        errors: list[str] = []
        try:
            # One upload job at a time: they share the same upstream bandwidth and quota.
            async with self._publish_lock:
                conn = await open_connection(self.settings.db_path)
                try:
                    try:
                        for cid in clip_ids:
                            try:
                                pub_res = await self.publisher.publish_clip(
                                    conn, cid, privacy_override=privacy
                                )
                                if int(pub_res.get("succeeded") or 0) > 0:
                                    posted_count += 1
                                else:
                                    failed_count += 1
                                    pub_errs = [
                                        str(p.get("error_message") or "")
                                        for p in pub_res.get("publications") or []
                                        if p.get("error_message")
                                    ]
                                    errors.append(
                                        f"clip {cid}: {pub_errs[0] if pub_errs else 'upload failed'}"
                                    )
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                logger.warning(
                                    "Telegram approve publish failed for clip %s: %s", cid, exc
                                )
                                await conn.execute(
                                    "UPDATE clip_drafts SET status = 'failed', updated_at = ? WHERE id = ?",
                                    (utcnow_iso(), cid),
                                )
                                await conn.commit()
                                failed_count += 1
                                errors.append(f"clip {cid}: {exc}")
                            remaining.remove(cid)
                            self._in_flight.discard(cid)
                            await self._refresh_chunk_message(conn, chunk_id, chat_id, message_id)
                    except asyncio.CancelledError:
                        await self._mark_interrupted(conn, remaining)
                        raise
                finally:
                    await conn.close()

            if errors and chat_id is not None:
                # The button reply was already sent, so report failures as a chat message.
                lines = [f"YouTube publish failed for {failed_count} clip(s):"]
                lines.extend(f"- {_clip_text(err, 300)}" for err in errors[:5])
                try:
                    payload: dict[str, Any] = {"chat_id": chat_id, "text": "\n".join(lines)}
                    if message_id is not None:
                        payload["reply_to_message_id"] = message_id
                    await self._api_call("sendMessage", payload, timeout=15.0)
                except TelegramBotError as exc:
                    logger.warning("Failed sending publish-failure notice: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Telegram publish job crashed for clips %s: %s", clip_ids, exc)
        finally:
            self._in_flight.difference_update(clip_ids)

        return {
            "clip_ids": list(clip_ids),
            "posted_count": posted_count,
            "failed_count": failed_count,
            "errors": errors,
        }

    async def _mark_interrupted(self, conn: aiosqlite.Connection, clip_ids: list[int]) -> None:
        """Mark clips whose publish was cut off by shutdown as failed so they can be re-approved."""
        if not clip_ids:
            return
        now = utcnow_iso()
        try:
            for cid in clip_ids:
                await conn.execute(
                    """
                    UPDATE publications
                    SET status = 'failed', error_message = 'Interrupted by backend shutdown',
                        updated_at = ?
                    WHERE clip_draft_id = ? AND status = 'uploading'
                    """,
                    (now, cid),
                )
                await conn.execute(
                    "UPDATE clip_drafts SET status = 'failed', updated_at = ? WHERE id = ? AND status = 'approved'",
                    (now, cid),
                )
            await conn.commit()
        except Exception as exc:
            logger.warning("Could not mark interrupted publishes %s: %s", clip_ids, exc)

    async def wait_for_publishes(self) -> list[dict[str, Any]]:
        """Wait for all background publish jobs started so far; returns their results."""
        results: list[dict[str, Any]] = []
        while self._publish_tasks:
            batch = list(self._publish_tasks)
            for res in await asyncio.gather(*batch, return_exceptions=True):
                if isinstance(res, dict):
                    results.append(res)
            self._publish_tasks.difference_update(batch)
        return results

    async def poll_once(self) -> list[dict[str, Any]]:
        """Fetch and handle one batch of Telegram callback_query updates."""
        if not self.is_configured():
            return []

        params: dict[str, Any] = {
            "timeout": self.poll_timeout_sec,
            "allowed_updates": ["callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset

        updates = await self._api_call(
            "getUpdates",
            params,
            timeout=float(self.poll_timeout_sec + 10),
        )
        if not isinstance(updates, list):
            return []

        results: list[dict[str, Any]] = []
        for upd in updates:
            if not isinstance(upd, dict):
                continue
            if "update_id" in upd:
                self._offset = int(upd["update_id"]) + 1
            try:
                results.append(await self.handle_update(upd))
            except Exception as exc:
                logger.exception("Error handling Telegram update: %s", exc)
        return results

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram getUpdates poll error: %s", exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

    async def start(self) -> None:
        """Start the background Telegram long-polling loop if configured."""
        if not self.is_configured():
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-bot")

    async def stop(self) -> None:
        """Stop the background Telegram long-polling loop cleanly."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Cancel in-flight uploads; _publish_job marks them failed so they can be re-approved.
        pending = list(self._publish_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._publish_tasks.clear()
