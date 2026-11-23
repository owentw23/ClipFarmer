from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import httpx
import websockets

from app.services.subprocess_compat import create_subprocess_exec

logger = logging.getLogger(__name__)

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_HELIX_STREAMS_URL = "https://api.twitch.tv/helix/streams"
TWITCH_EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
TWITCH_HELIX_BATCH_SIZE = 100


def _resolve_executable(name: str) -> str | None:
    """Resolve an executable inside the current Python venv first, then system PATH."""
    exe_dir = Path(sys.executable).resolve().parent
    for candidate_name in (f"{name}.exe", name):
        candidate = exe_dir / candidate_name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def _normalize_login(login: str) -> str:
    return login.strip().lstrip("@").lower()


class TwitchWatcher:
    """Checks Twitch channel live status via Helix API with streamlink fallback."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        http_client: httpx.AsyncClient | None = None,
        max_rate_limit_wait: float = 5.0,
        streamlink_concurrency: int = 4,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._http_client = http_client
        self._max_rate_limit_wait = max_rate_limit_wait
        self._streamlink_concurrency = max(1, streamlink_concurrency)
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self.last_probe_errors: set[str] = set()

    @property
    def has_credentials(self) -> bool:
        """Return True when both Twitch client_id and client_secret are configured."""
        return bool(self.client_id and self.client_secret)

    async def _request_app_token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            TWITCH_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        token = str(payload["access_token"])
        expires_in = float(payload.get("expires_in", 3600))
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(0.0, expires_in)
        return token

    async def get_app_access_token(
        self,
        *,
        force_refresh: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        """Return a cached App Access Token or fetch a new one via client_credentials."""
        if not self.has_credentials:
            raise ValueError("Twitch client_id and client_secret are required for Helix API")

        if (
            not force_refresh
            and self._access_token
            and time.monotonic() < (self._token_expires_at - 60.0)
        ):
            return self._access_token

        async with self._token_lock:
            if (
                not force_refresh
                and self._access_token
                and time.monotonic() < (self._token_expires_at - 60.0)
            ):
                return self._access_token

            if client is not None:
                return await self._request_app_token(client)
            if self._http_client is not None:
                return await self._request_app_token(self._http_client)
            async with httpx.AsyncClient(timeout=15.0) as temp_client:
                return await self._request_app_token(temp_client)

    async def _check_batch_helix(
        self,
        client: httpx.AsyncClient,
        batch: list[str],
    ) -> list[dict[str, Any]]:
        params = [("user_login", login) for login in batch]
        token = await self.get_app_access_token(client=client)
        retried_auth = False
        retried_rate_limit = False

        while True:
            resp = await client.get(
                TWITCH_HELIX_STREAMS_URL,
                params=params,
                headers={
                    "Client-Id": self.client_id,
                    "Authorization": f"Bearer {token}",
                },
            )
            if resp.status_code == 401 and not retried_auth:
                retried_auth = True
                token = await self.get_app_access_token(force_refresh=True, client=client)
                continue

            if resp.status_code == 429:
                if not retried_rate_limit:
                    retried_rate_limit = True
                    wait_sec = self._compute_rate_limit_wait(resp)
                    logger.warning("Twitch Helix rate limited (429); retrying after %.2fs", wait_sec)
                    if wait_sec > 0:
                        await asyncio.sleep(wait_sec)
                    continue
                logger.warning("Twitch Helix rate limit persisted (429) for batch of %d", len(batch))
                self.last_probe_errors.update(batch)
                return []

            resp.raise_for_status()
            data = resp.json().get("data", [])
            return data if isinstance(data, list) else []

    def _compute_rate_limit_wait(self, resp: httpx.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(0.0, float(retry_after)), self._max_rate_limit_wait)
            except ValueError:
                pass
        reset_at = resp.headers.get("Ratelimit-Reset")
        if reset_at:
            try:
                delta = float(reset_at) - time.time()
                return min(max(0.1, delta), self._max_rate_limit_wait)
            except ValueError:
                pass
        return min(1.0, self._max_rate_limit_wait)

    async def _check_channel_streamlink(self, login: str) -> dict[str, Any] | None:
        exe = _resolve_executable("streamlink")
        if not exe:
            logger.warning("streamlink executable not found; cannot probe twitch.tv/%s", login)
            return None

        url = f"https://twitch.tv/{login}"
        proc = None
        try:
            proc = await create_subprocess_exec(
                exe,
                "--json",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            self.last_probe_errors.add(login)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except Exception:
                    pass
            logger.warning("streamlink --json timed out for %s", login)
            return None
        except Exception as exc:
            self.last_probe_errors.add(login)
            logger.warning("streamlink --json failed for %s: %s", login, exc)
            return None

        if proc.returncode != 0 or not stdout_bytes:
            return None

        try:
            payload = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

        streams = payload.get("streams")
        if not isinstance(streams, dict) or not streams:
            return None

        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        return {
            "external_stream_id": str(metadata.get("id") or f"streamlink:{login}"),
            "title": str(metadata.get("title") or ""),
            "stream_url": f"https://www.twitch.tv/{login}",
            "started_at": "",
            "viewer_count": 0,
        }

    async def _check_channels_streamlink(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        semaphore = asyncio.Semaphore(self._streamlink_concurrency)

        async def _probe(login: str) -> tuple[str, dict[str, Any] | None]:
            async with semaphore:
                return login, await self._check_channel_streamlink(login)

        pairs = await asyncio.gather(*(_probe(login) for login in logins))
        return {login: info for login, info in pairs if info is not None}

    async def check_channels(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        """Check live status for Twitch channel logins and return live entries keyed by lowercase login."""
        self.last_probe_errors = set()
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in logins:
            login = _normalize_login(raw)
            if login and login not in seen:
                seen.add(login)
                normalized.append(login)

        if not normalized:
            return {}

        if not self.has_credentials:
            return await self._check_channels_streamlink(normalized)

        results: dict[str, dict[str, Any]] = {}

        async def _run_with_client(client: httpx.AsyncClient) -> None:
            for offset in range(0, len(normalized), TWITCH_HELIX_BATCH_SIZE):
                batch = normalized[offset : offset + TWITCH_HELIX_BATCH_SIZE]
                items = await self._check_batch_helix(client, batch)
                for item in items:
                    login_lower = str(
                        item.get("user_login") or item.get("user_name") or ""
                    ).lower()
                    if not login_lower:
                        continue
                    results[login_lower] = {
                        "external_stream_id": str(item.get("id") or ""),
                        "title": str(item.get("title") or ""),
                        "stream_url": f"https://www.twitch.tv/{login_lower}",
                        "started_at": str(item.get("started_at") or ""),
                        "viewer_count": int(item.get("viewer_count") or 0),
                    }

        if self._http_client is not None:
            await _run_with_client(self._http_client)
        else:
            async with httpx.AsyncClient(timeout=15.0) as temp_client:
                await _run_with_client(temp_client)

        return results


class TwitchEventSubClient:
    """Optional Twitch EventSub WebSocket client that falls back gracefully to Helix polling."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        on_online: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_offline: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        ws_url: str = TWITCH_EVENTSUB_WS_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.on_online = on_online
        self.on_offline = on_offline
        self.ws_url = ws_url
        self._http_client = http_client
        self.session_id: str | None = None
        self.connected: bool = False
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def handle_message(self, raw: str | dict[str, Any]) -> str | None:
        """Process one EventSub WebSocket message and return its message_type."""
        data = json.loads(raw) if isinstance(raw, str) else raw
        metadata = data.get("metadata") or {}
        payload = data.get("payload") or {}
        msg_type = metadata.get("message_type")

        if msg_type == "session_welcome":
            session = payload.get("session") or {}
            self.session_id = session.get("id")
            self.connected = bool(self.session_id)
        elif msg_type == "session_reconnect":
            session = payload.get("session") or {}
            reconnect_url = session.get("reconnect_url")
            if reconnect_url:
                self.ws_url = str(reconnect_url)
        elif msg_type == "notification":
            subscription = payload.get("subscription") or {}
            sub_type = subscription.get("type")
            event = payload.get("event") or {}
            if sub_type == "stream.online" and self.on_online is not None:
                res = self.on_online(event)
                if asyncio.iscoroutine(res):
                    await res
            elif sub_type == "stream.offline" and self.on_offline is not None:
                res = self.on_offline(event)
                if asyncio.iscoroutine(res):
                    await res

        return str(msg_type) if msg_type is not None else None

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url, open_timeout=10.0) as ws:
                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue
                        msg_type = await self.handle_message(raw)
                        if msg_type == "session_reconnect":
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.session_id = None
                logger.info("Twitch EventSub unavailable (%s); continuing with Helix polling", exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    continue

        self.connected = False
        self.session_id = None

    async def start(self) -> None:
        """Start the EventSub listener task if credentials are configured."""
        if not self.is_configured:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="twitch-eventsub")

    async def stop(self) -> None:
        """Stop the EventSub listener task cleanly."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.connected = False
        self.session_id = None
