from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from app.config import Settings
from app.db import utcnow_iso
from app.services.google_oauth import GoogleOAuthError, get_valid_access_token

logger = logging.getLogger(__name__)

VALID_PRIVACY_STATUSES = {"public", "unlisted", "private"}
YOUTUBE_UPLOAD_URL = (
    "https://www.googleapis.com/upload/youtube/v3/videos"
    "?uploadType=resumable&part=snippet,status"
)
RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB (multiple of 256 KB)


class YouTubePublisherError(RuntimeError):
    """Raised when YouTube publishing configuration or upload requests fail."""


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


class YouTubePublisherService:
    """Async YouTube Data API v3 resumable uploader with target routing and retries."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        *,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = max(0.0, float(retry_base_delay))

    async def list_publications(
        self,
        db: aiosqlite.Connection,
        clip_id: int,
    ) -> list[dict[str, Any]]:
        """Return publication records for a clip draft ordered newest first."""
        async with db.execute(
            """
            SELECT p.id,
                   p.clip_draft_id,
                   p.youtube_account_id,
                   ya.channel_id,
                   ya.channel_title,
                   p.youtube_video_id,
                   p.youtube_url,
                   p.privacy_status,
                   p.status,
                   p.published_at,
                   p.error_message,
                   p.created_at,
                   p.updated_at
            FROM publications p
            LEFT JOIN youtube_accounts ya ON ya.id = p.youtube_account_id
            WHERE p.clip_draft_id = ?
            ORDER BY p.id DESC
            """,
            (clip_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def resolve_targets(
        self,
        db: aiosqlite.Connection,
        streamer_id: int,
        account_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve enabled YouTube publishing targets for a streamer."""
        if account_ids is not None and len(account_ids) == 0:
            return []

        requested_ids: set[int] | None = (
            {int(aid) for aid in account_ids} if account_ids is not None else None
        )

        async with db.execute(
            """
            SELECT st.id AS target_id,
                   st.streamer_id,
                   st.youtube_account_id,
                   st.default_privacy,
                   st.default_tags_json,
                   ya.channel_id,
                   ya.channel_title,
                   ya.oauth_provider,
                   ya.scopes,
                   ya.status AS account_status
            FROM streamer_targets st
            JOIN youtube_accounts ya ON ya.id = st.youtube_account_id
            WHERE st.streamer_id = ?
              AND st.enabled = 1
              AND ya.status = 'active'
              AND COALESCE(ya.oauth_provider, 'google') != 'google_drive'
              AND ya.channel_id NOT LIKE 'drive-only:%'
            ORDER BY st.id ASC
            """,
            (streamer_id,),
        ) as cur:
            rows = await cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            if requested_ids is not None and int(data["youtube_account_id"]) not in requested_ids:
                continue
            scopes = str(data.get("scopes") or "").strip()
            if scopes and "youtube.upload" not in scopes:
                continue
            results.append(data)
        return results

    async def _send_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        expected_statuses: tuple[int, ...],
        failure_label: str,
        **kwargs: Any,
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    if self.retry_base_delay > 0:
                        await asyncio.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise YouTubePublisherError(f"{failure_label} transport error: {exc}") from exc

            if resp.status_code in expected_statuses:
                return resp

            if _is_retryable_status(resp.status_code) and attempt < self.max_retries:
                if self.retry_base_delay > 0:
                    await asyncio.sleep(self.retry_base_delay * (2**attempt))
                continue

            raise YouTubePublisherError(
                f"{failure_label} failed ({resp.status_code}): {resp.text[:500]}"
            )

        raise YouTubePublisherError(f"{failure_label} exhausted retries")

    async def _upload_video(
        self,
        *,
        access_token: str,
        file_path: Path,
        metadata: dict[str, Any],
    ) -> str:
        file_size = file_path.stat().st_size
        init_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        }

        async def _execute(client: httpx.AsyncClient) -> str:
            init_resp = await self._send_with_retry(
                client,
                "POST",
                YOUTUBE_UPLOAD_URL,
                expected_statuses=(200, 201),
                failure_label="YouTube resumable upload init",
                headers=init_headers,
                json=metadata,
            )

            session_uri = (
                init_resp.headers.get("Location") or init_resp.headers.get("location") or ""
            ).strip()
            if not session_uri.startswith("https://"):
                raise YouTubePublisherError(
                    "YouTube resumable upload response missing valid HTTPS Location header"
                )

            upload_data: dict[str, Any] = {}
            if file_size == 0:
                put_resp = await self._send_with_retry(
                    client,
                    "PUT",
                    session_uri,
                    expected_statuses=(200, 201),
                    failure_label="YouTube empty video upload",
                    headers={
                        "Content-Length": "0",
                        "Content-Type": "video/mp4",
                    },
                    content=b"",
                )
                upload_data = put_resp.json()
            else:
                offset = 0
                with file_path.open("rb") as fh:
                    while offset < file_size:
                        chunk = fh.read(RESUMABLE_CHUNK_SIZE)
                        if not chunk:
                            break
                        end_byte = offset + len(chunk) - 1
                        put_headers = {
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                            "Content-Type": "video/mp4",
                        }
                        put_resp = await self._send_with_retry(
                            client,
                            "PUT",
                            session_uri,
                            expected_statuses=(200, 201, 308),
                            failure_label="YouTube video chunk upload",
                            headers=put_headers,
                            content=chunk,
                        )
                        offset += len(chunk)
                        if put_resp.status_code in (200, 201):
                            upload_data = put_resp.json()

            video_id = str(upload_data.get("id") or "").strip()
            if not video_id:
                raise YouTubePublisherError(
                    "YouTube upload completed without returning a video id"
                )
            return video_id

        if self._http_client is not None:
            return await _execute(self._http_client)

        async with httpx.AsyncClient(timeout=120.0) as client:
            return await _execute(client)

    async def publish_clip(
        self,
        db: aiosqlite.Connection,
        clip_id: int,
        *,
        privacy_override: str | None = None,
        account_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Publish an approved clip draft to each resolved YouTube target channel."""
        async with db.execute("SELECT * FROM clip_drafts WHERE id = ?", (clip_id,)) as cur:
            clip = await cur.fetchone()
        if clip is None:
            raise LookupError(f"Clip draft {clip_id} not found")

        if privacy_override is not None and privacy_override not in VALID_PRIVACY_STATUSES:
            raise ValueError(f"Invalid privacy status: {privacy_override}")

        raw_path = str(clip["file_path"] or "").strip()
        if not raw_path:
            raise FileNotFoundError(f"Clip {clip_id} has no file_path")

        drafts_root = Path(self.settings.drafts_dir).resolve()
        file_path = Path(raw_path).resolve()
        if not file_path.is_relative_to(drafts_root):
            raise ValueError(f"Clip file path is outside drafts_dir: {file_path}")
        if not file_path.is_file():
            raise FileNotFoundError(f"Clip video file not found: {file_path}")

        targets = await self.resolve_targets(
            db,
            int(clip["streamer_id"]),
            account_ids=account_ids,
        )
        if not targets:
            raise YouTubePublisherError(
                "No enabled YouTube publishing targets configured for streamer"
            )

        try:
            parsed_tags = json.loads(clip["tags_json"] or "[]")
        except json.JSONDecodeError:
            parsed_tags = []
        tags = (
            [str(tag) for tag in parsed_tags if tag is not None]
            if isinstance(parsed_tags, list)
            else []
        )

        succeeded = 0
        failed = 0
        created_pub_ids: set[int] = set()

        for target in targets:
            effective_privacy = privacy_override or target.get("default_privacy") or "public"
            if effective_privacy not in VALID_PRIVACY_STATUSES:
                raise ValueError(f"Invalid privacy status: {effective_privacy}")

            now = utcnow_iso()
            cur = await db.execute(
                """
                INSERT INTO publications (
                    clip_draft_id, youtube_account_id, privacy_status,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, 'uploading', ?, ?)
                """,
                (
                    clip_id,
                    int(target["youtube_account_id"]),
                    effective_privacy,
                    now,
                    now,
                ),
            )
            await db.commit()
            pub_id = int(cur.lastrowid)
            created_pub_ids.add(pub_id)

            metadata = {
                "snippet": {
                    "title": (str(clip["title"] or "") or f"Clip {clip_id}")[:100],
                    "description": str(clip["description"] or "")[:5000],
                    "tags": tags,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": effective_privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }

            try:
                access_token = await get_valid_access_token(
                    db,
                    self.settings,
                    int(target["youtube_account_id"]),
                    http_client=self._http_client,
                )
                video_id = await self._upload_video(
                    access_token=access_token,
                    file_path=file_path,
                    metadata=metadata,
                )
                done_at = utcnow_iso()
                youtube_url = f"https://www.youtube.com/shorts/{video_id}"
                await db.execute(
                    """
                    UPDATE publications
                    SET youtube_video_id = ?,
                        youtube_url = ?,
                        status = 'published',
                        published_at = ?,
                        error_message = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (video_id, youtube_url, done_at, done_at, pub_id),
                )
                await db.commit()
                succeeded += 1
            except (GoogleOAuthError, YouTubePublisherError, httpx.HTTPError, ValueError) as exc:
                err_at = utcnow_iso()
                logger.warning(
                    "YouTube upload failed for clip %s account %s: %s",
                    clip_id,
                    target["youtube_account_id"],
                    exc,
                )
                await db.execute(
                    """
                    UPDATE publications
                    SET status = 'failed',
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc)[:1000], err_at, pub_id),
                )
                await db.commit()
                failed += 1

        final_status = "posted" if succeeded > 0 else "failed"
        await db.execute(
            "UPDATE clip_drafts SET status = ?, updated_at = ? WHERE id = ?",
            (final_status, utcnow_iso(), clip_id),
        )
        await db.commit()

        all_pubs = await self.list_publications(db, clip_id)
        target_pub_rows = [row for row in all_pubs if int(row["id"]) in created_pub_ids]
        return {
            "clip_id": clip_id,
            "status": final_status,
            "publications": target_pub_rows,
            "succeeded": succeeded,
            "failed": failed,
        }
