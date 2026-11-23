from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
import random
from typing import Any

import aiosqlite
import httpx

from app.config import Settings
from app.services.google_oauth import get_valid_access_token

logger = logging.getLogger(__name__)

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE_FRAGMENT = "drive.file"
RESUMABLE_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB (exact multiple of 256 KB)
_DRIVE_RATE_LIMIT_REASONS = (
    "ratelimitexceeded",
    "userratelimitexceeded",
    "sharingratelimitexceeded",
    "backenderror",
)


def _is_retryable_drive_response(resp: httpx.Response) -> bool:
    """Return True for Drive HTTP 429, 5xx, or 403 rate-limit responses."""
    if resp.status_code in (429, 500, 502, 503, 504):
        return True
    if resp.status_code == 403:
        lower = (resp.text or "").lower()
        return any(reason in lower for reason in _DRIVE_RATE_LIMIT_REASONS)
    return False


def _retry_delay_seconds(
    resp: httpx.Response | None,
    attempt: int,
    base_delay: float = 0.5,
) -> float:
    """Compute backoff delay, honoring numeric Retry-After when present."""
    if base_delay <= 0:
        return 0.0
    if resp is not None:
        raw_after = resp.headers.get("Retry-After")
        if raw_after:
            try:
                parsed = float(raw_after.strip())
                if parsed >= 0:
                    return min(15.0, max(0.1, parsed))
            except ValueError:
                pass
    return min(15.0, base_delay * (2**attempt) + random.uniform(0.0, 0.25))


class GoogleDriveError(RuntimeError):
    """Raised when Google Drive API requests fail."""


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveService:
    """Async Google Drive v3 client for folder hierarchy creation, resumable upload, and deletion."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client
        self._folder_cache: dict[tuple[str, str], str] = {}

    async def resolve_drive_account_id(
        self,
        db: aiosqlite.Connection,
        preferred_account_id: int | None = None,
    ) -> int | None:
        """Find an active Google account row that has the drive.file scope."""
        if preferred_account_id is not None:
            async with db.execute(
                "SELECT id, scopes, status FROM youtube_accounts WHERE id = ?",
                (preferred_account_id,),
            ) as cur:
                preferred = await cur.fetchone()
            if (
                preferred is not None
                and preferred["status"] == "active"
                and DRIVE_SCOPE_FRAGMENT in (preferred["scopes"] or "")
            ):
                return int(preferred["id"])

        async with db.execute(
            """
            SELECT id, scopes FROM youtube_accounts
            WHERE status = 'active'
            ORDER BY CASE WHEN oauth_provider = 'google_drive' THEN 0 ELSE 1 END, id ASC
            """
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            if DRIVE_SCOPE_FRAGMENT in (row["scopes"] or ""):
                return int(row["id"])
        return None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> httpx.Response:
        client_to_use = http_client or self._http_client
        if client_to_use is not None:
            return await client_to_use.request(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, **kwargs)

    async def ensure_folder_path(
        self,
        access_token: str,
        folder_names: list[str],
        http_client: httpx.AsyncClient | None = None,
    ) -> str:
        """Ensure a nested folder hierarchy under Drive root and return the leaf folder ID."""
        parent_id = "root"
        headers = {"Authorization": f"Bearer {access_token}"}

        for raw_name in folder_names:
            name = (raw_name or "").strip()
            if not name:
                continue

            cache_key = (parent_id, name)
            cached_id = self._folder_cache.get(cache_key)
            if cached_id is not None:
                parent_id = cached_id
                continue

            escaped = _escape_drive_query_value(name)
            query = (
                f"name = '{escaped}' "
                f"and mimeType = '{DRIVE_FOLDER_MIME}' "
                f"and '{parent_id}' in parents "
                "and trashed = false"
            )
            list_resp = await self._request(
                "GET",
                DRIVE_FILES_URL,
                http_client=http_client,
                headers=headers,
                params={
                    "q": query,
                    "fields": "files(id, name)",
                    "pageSize": 1,
                    "spaces": "drive",
                },
            )
            if list_resp.status_code >= 400:
                raise GoogleDriveError(
                    f"Drive folder lookup failed ({list_resp.status_code}): {list_resp.text}"
                )

            files = list_resp.json().get("files") or []
            if files:
                folder_id = str(files[0]["id"])
            else:
                create_resp = await self._request(
                    "POST",
                    DRIVE_FILES_URL,
                    http_client=http_client,
                    headers=headers,
                    params={"fields": "id"},
                    json={
                        "name": name,
                        "mimeType": DRIVE_FOLDER_MIME,
                        "parents": [parent_id],
                    },
                )
                if create_resp.status_code >= 400:
                    raise GoogleDriveError(
                        f"Drive folder creation failed ({create_resp.status_code}): {create_resp.text}"
                    )
                folder_id = str(create_resp.json()["id"])

            self._folder_cache[cache_key] = folder_id
            parent_id = folder_id

        return parent_id

    async def upload_clip(
        self,
        db: aiosqlite.Connection,
        file_path: str | Path,
        *,
        streamer_name: str,
        clip_date: str,
        display_name: str | None = None,
        account_id: int | None = None,
        share_anyone: bool = True,
    ) -> dict[str, str] | None:
        """Upload a clip via Drive v3 resumable upload; returns None if no Drive account exists."""
        src = Path(file_path)
        if not src.is_file():
            raise FileNotFoundError(f"Clip file does not exist: {src}")

        resolved_account_id = await self.resolve_drive_account_id(
            db, preferred_account_id=account_id
        )
        if resolved_account_id is None:
            return None

        access_token = await get_valid_access_token(
            db,
            self.settings,
            resolved_account_id,
            http_client=self._http_client,
        )
        folder_id = await self.ensure_folder_path(
            access_token,
            ["ClipPipeline", streamer_name or "Unknown", clip_date or "unknown-date"],
            http_client=self._http_client,
        )

        file_size = src.stat().st_size
        upload_name = (display_name or "").strip() or src.name
        mime_type = mimetypes.guess_type(src.name)[0] or "video/mp4"

        init_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(file_size),
        }
        init_resp = await self._request(
            "POST",
            f"{DRIVE_UPLOAD_URL}?uploadType=resumable&fields=id,webViewLink,webContentLink",
            http_client=self._http_client,
            headers=init_headers,
            json={"name": upload_name, "parents": [folder_id]},
        )
        if init_resp.status_code >= 400:
            raise GoogleDriveError(
                f"Drive resumable upload init failed ({init_resp.status_code}): {init_resp.text}"
            )

        session_uri = init_resp.headers.get("Location") or init_resp.headers.get("location")
        if not session_uri:
            raise GoogleDriveError("Drive resumable upload response missing Location header")

        upload_data: dict[str, Any] = {}
        if file_size == 0:
            put_resp = await self._request(
                "PUT",
                session_uri,
                http_client=self._http_client,
                headers={"Content-Length": "0", "Content-Type": mime_type},
                content=b"",
            )
            if put_resp.status_code not in (200, 201):
                raise GoogleDriveError(
                    f"Drive empty upload failed ({put_resp.status_code}): {put_resp.text}"
                )
            upload_data = put_resp.json()
        else:
            offset = 0
            with src.open("rb") as fh:
                while offset < file_size:
                    chunk = fh.read(RESUMABLE_CHUNK_SIZE)
                    if not chunk:
                        break
                    end_byte = offset + len(chunk) - 1
                    put_headers = {
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                        "Content-Type": mime_type,
                    }
                    put_resp = await self._request(
                        "PUT",
                        session_uri,
                        http_client=self._http_client,
                        timeout=120.0,
                        headers=put_headers,
                        content=chunk,
                    )
                    if put_resp.status_code not in (200, 201, 308):
                        raise GoogleDriveError(
                            f"Drive chunk upload failed ({put_resp.status_code}): {put_resp.text}"
                        )
                    offset += len(chunk)
                    if put_resp.status_code in (200, 201):
                        upload_data = put_resp.json()

        file_id = str(upload_data.get("id") or "")
        if not file_id:
            raise GoogleDriveError("Drive upload completed without returning a file id")

        if share_anyone:
            perm_resp = await self._request(
                "POST",
                f"{DRIVE_FILES_URL}/{file_id}/permissions",
                http_client=self._http_client,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"role": "reader", "type": "anyone"},
            )
            if perm_resp.status_code >= 400:
                raise GoogleDriveError(
                    f"Drive permission creation failed ({perm_resp.status_code}): {perm_resp.text}"
                )

        web_view_link = upload_data.get("webViewLink")
        return {
            "drive_file_id": file_id,
            "drive_url": web_view_link or f"https://drive.google.com/file/d/{file_id}/view",
        }

    async def delete_file(
        self,
        db: aiosqlite.Connection,
        file_id: str,
        account_id: int | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 4,
        base_retry_delay: float = 0.5,
    ) -> bool:
        """Delete a Drive file by ID with backoff retry on 429/403 rate-limit/5xx.

        Returns True on 200/204/404 and False otherwise.
        """
        if not file_id:
            return False

        resolved_account_id = await self.resolve_drive_account_id(
            db, preferred_account_id=account_id
        )
        if resolved_account_id is None:
            return False

        client_to_use = http_client or self._http_client
        attempts = max(1, int(max_retries))

        try:
            access_token = await get_valid_access_token(
                db,
                self.settings,
                resolved_account_id,
                http_client=client_to_use,
            )
        except Exception as exc:
            logger.warning(
                "Drive token fetch failed for %s (%s: %s)",
                file_id,
                type(exc).__name__,
                exc,
            )
            return False

        for attempt in range(attempts):
            try:
                resp = await self._request(
                    "DELETE",
                    f"{DRIVE_FILES_URL}/{file_id}",
                    http_client=client_to_use,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt + 1 < attempts:
                    delay = _retry_delay_seconds(None, attempt, base_retry_delay)
                    logger.warning(
                        "Drive delete transport error for %s (attempt %d/%d: %s); retrying in %.2fs",
                        file_id,
                        attempt + 1,
                        attempts,
                        type(exc).__name__,
                        delay,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "Drive delete failed for %s (%s: %s)",
                    file_id,
                    type(exc).__name__,
                    exc,
                )
                return False
            except Exception as exc:
                logger.warning(
                    "Drive delete failed for %s (%s: %s)",
                    file_id,
                    type(exc).__name__,
                    exc,
                )
                return False

            if resp.status_code in (200, 204, 404):
                return True

            if _is_retryable_drive_response(resp) and attempt + 1 < attempts:
                delay = _retry_delay_seconds(resp, attempt, base_retry_delay)
                logger.warning(
                    "Drive delete rate-limited/transient HTTP %s for %s (attempt %d/%d); retrying in %.2fs",
                    resp.status_code,
                    file_id,
                    attempt + 1,
                    attempts,
                    delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                continue

            logger.warning(
                "Drive delete returned HTTP %s for %s: %s",
                resp.status_code,
                file_id,
                resp.text,
            )
            return False

        return False
