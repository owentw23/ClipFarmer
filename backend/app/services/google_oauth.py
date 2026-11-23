from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import aiosqlite
import httpx

from app.config import Settings
from app.security import decrypt_token, encrypt_token

GOOGLE_OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
)

GOOGLE_DRIVE_OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.email",
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

TOKEN_REFRESH_BUFFER_SECONDS = 120


class GoogleOAuthError(RuntimeError):
    """Raised when Google OAuth token exchange, refresh, or profile fetch fails."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def compute_token_expires_at(expires_in: int | float | str | None) -> str | None:
    """Compute an ISO-8601 UTC expiration timestamp from an expires_in offset."""
    if expires_in is None:
        return None
    seconds = int(expires_in)
    return (_utcnow() + timedelta(seconds=seconds)).isoformat()


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def serialize_youtube_account(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    """Serialize a youtube_accounts row without exposing encrypted tokens."""
    data = dict(row)
    oauth_provider = str(data.get("oauth_provider") or "google")
    return {
        "id": data["id"],
        "provider": "google_drive" if oauth_provider == "google_drive" else "youtube",
        "oauth_provider": oauth_provider,
        "channel_id": data["channel_id"],
        "channel_title": data["channel_title"],
        "email": data.get("email"),
        "scopes": data.get("scopes"),
        "status": data["status"],
        "token_expires_at": data.get("token_expires_at"),
        "has_refresh_token": bool(data.get("encrypted_refresh_token")),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
    }


def build_google_auth_url(
    settings: Settings,
    state: str,
    *,
    client_id: str | None = None,
    redirect_uri: str | None = None,
    scopes: tuple[str, ...] | None = None,
) -> str:
    """Build a Google OAuth 2.0 consent URL requesting offline access."""
    params = {
        "client_id": client_id if client_id is not None else settings.google_client_id,
        "redirect_uri": (
            redirect_uri if redirect_uri is not None else settings.google_redirect_uri
        ),
        "response_type": "code",
        "scope": " ".join(scopes if scopes is not None else GOOGLE_OAUTH_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def build_google_drive_auth_url(settings: Settings, state: str) -> str:
    """Build the dedicated Google Drive OAuth 2.0 consent URL."""
    return build_google_auth_url(
        settings,
        state,
        client_id=settings.google_drive_client_id,
        redirect_uri=settings.google_drive_redirect_uri,
        scopes=GOOGLE_DRIVE_OAUTH_SCOPES,
    )


async def _post_token_request(
    payload: dict[str, str],
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if http_client is not None:
        response = await http_client.post(GOOGLE_TOKEN_URL, data=payload)
    else:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(GOOGLE_TOKEN_URL, data=payload)

    if response.status_code >= 400:
        raise GoogleOAuthError(
            f"Google token endpoint returned {response.status_code}: {response.text}"
        )

    data = response.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise GoogleOAuthError("Google token response did not contain access_token")
    if "expires_in" in data and "expires_at" not in data:
        data["expires_at"] = compute_token_expires_at(data.get("expires_in"))
    return data


async def exchange_code_for_tokens(
    settings: Settings,
    code: str,
    http_client: httpx.AsyncClient | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Exchange an authorization code for Google access and refresh tokens."""
    return await _post_token_request(
        {
            "client_id": client_id if client_id is not None else settings.google_client_id,
            "client_secret": (
                client_secret
                if client_secret is not None
                else settings.google_client_secret
            ),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": (
                redirect_uri if redirect_uri is not None else settings.google_redirect_uri
            ),
        },
        http_client=http_client,
    )


async def exchange_drive_code_for_tokens(
    settings: Settings,
    code: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Exchange a Google Drive OAuth authorization code using dedicated Drive client creds."""
    return await exchange_code_for_tokens(
        settings,
        code,
        http_client=http_client,
        client_id=settings.google_drive_client_id,
        client_secret=settings.google_drive_client_secret,
        redirect_uri=settings.google_drive_redirect_uri,
    )


async def refresh_access_token(
    settings: Settings,
    encrypted_refresh_token: str,
    settings_key: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Decrypt a stored refresh token and exchange it for a fresh Google access token."""
    enc_key = settings_key if settings_key is not None else settings.token_encryption_key
    refresh_token = decrypt_token(encrypted_refresh_token, key=enc_key)
    return await _post_token_request(
        {
            "client_id": client_id if client_id is not None else settings.google_client_id,
            "client_secret": (
                client_secret
                if client_secret is not None
                else settings.google_client_secret
            ),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        http_client=http_client,
    )


async def fetch_google_userinfo(
    access_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch only the authenticated Google user's email and user ID (no YouTube API call)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if http_client is not None:
        user_resp = await http_client.get(GOOGLE_USERINFO_URL, headers=headers)
    else:
        async with httpx.AsyncClient(timeout=20.0) as client:
            user_resp = await client.get(GOOGLE_USERINFO_URL, headers=headers)

    if user_resp.status_code >= 400:
        raise GoogleOAuthError(
            f"Google userinfo endpoint returned {user_resp.status_code}: {user_resp.text}"
        )

    user_data = user_resp.json()
    return {
        "email": user_data.get("email"),
        "user_id": user_data.get("id") or user_data.get("sub"),
    }


async def fetch_google_user_and_channels(
    access_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch the authenticated Google user's email and owned YouTube channels."""
    headers = {"Authorization": f"Bearer {access_token}"}

    async def _do_fetch(client: httpx.AsyncClient) -> tuple[httpx.Response, httpx.Response]:
        user_resp = await client.get(GOOGLE_USERINFO_URL, headers=headers)
        yt_resp = await client.get(
            YOUTUBE_CHANNELS_URL,
            params={"part": "snippet", "mine": "true"},
            headers=headers,
        )
        return user_resp, yt_resp

    if http_client is not None:
        user_resp, yt_resp = await _do_fetch(http_client)
    else:
        async with httpx.AsyncClient(timeout=20.0) as client:
            user_resp, yt_resp = await _do_fetch(client)

    if user_resp.status_code >= 400:
        raise GoogleOAuthError(
            f"Google userinfo endpoint returned {user_resp.status_code}: {user_resp.text}"
        )
    if yt_resp.status_code >= 400:
        raise GoogleOAuthError(
            f"YouTube channels endpoint returned {yt_resp.status_code}: {yt_resp.text}"
        )

    user_data = user_resp.json()
    yt_data = yt_resp.json()

    email = user_data.get("email")
    user_id = user_data.get("id") or user_data.get("sub")
    channels: list[dict[str, str]] = []
    for item in yt_data.get("items") or []:
        channel_id = item.get("id")
        if not channel_id:
            continue
        snippet = item.get("snippet") or {}
        channel_title = snippet.get("title") or channel_id
        channels.append(
            {
                "channel_id": str(channel_id),
                "channel_title": str(channel_title),
            }
        )

    return {
        "email": email,
        "user_id": user_id,
        "channels": channels,
    }


async def _upsert_account_rows(
    db: aiosqlite.Connection,
    settings: Settings,
    token_data: dict[str, Any],
    *,
    email: str | None,
    channels: list[dict[str, str]],
    oauth_provider: str,
    default_scopes: tuple[str, ...],
) -> list[dict[str, Any]]:
    enc_key = settings.token_encryption_key
    access_token = token_data["access_token"]
    encrypted_access = encrypt_token(access_token, key=enc_key)

    raw_refresh = token_data.get("refresh_token")
    encrypted_refresh = (
        encrypt_token(raw_refresh, key=enc_key) if raw_refresh else None
    )

    expires_at = token_data.get("expires_at") or compute_token_expires_at(
        token_data.get("expires_in")
    )
    scopes = token_data.get("scope") or " ".join(default_scopes)
    now = _utcnow().isoformat()
    account_ids: list[int] = []

    for channel in channels:
        channel_id = channel["channel_id"]
        channel_title = channel["channel_title"]
        async with db.execute(
            "SELECT id, email, encrypted_refresh_token FROM youtube_accounts WHERE channel_id = ?",
            (channel_id,),
        ) as cur:
            existing = await cur.fetchone()

        if existing is not None:
            preserved_refresh = encrypted_refresh or existing["encrypted_refresh_token"]
            merged_email = email if email is not None else existing["email"]
            await db.execute(
                """
                UPDATE youtube_accounts
                SET channel_title = ?,
                    email = ?,
                    oauth_provider = ?,
                    encrypted_refresh_token = ?,
                    encrypted_access_token = ?,
                    token_expires_at = ?,
                    scopes = ?,
                    status = 'active',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    channel_title,
                    merged_email,
                    oauth_provider,
                    preserved_refresh,
                    encrypted_access,
                    expires_at,
                    scopes,
                    now,
                    existing["id"],
                ),
            )
            account_ids.append(int(existing["id"]))
        else:
            cursor = await db.execute(
                """
                INSERT INTO youtube_accounts (
                    channel_id, channel_title, email, oauth_provider,
                    encrypted_refresh_token, encrypted_access_token,
                    token_expires_at, scopes, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    channel_id,
                    channel_title,
                    email,
                    oauth_provider,
                    encrypted_refresh,
                    encrypted_access,
                    expires_at,
                    scopes,
                    now,
                    now,
                ),
            )
            account_ids.append(int(cursor.lastrowid))

    await db.commit()

    results: list[dict[str, Any]] = []
    for account_id in account_ids:
        async with db.execute(
            "SELECT * FROM youtube_accounts WHERE id = ?",
            (account_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            results.append(serialize_youtube_account(row))
    return results


async def upsert_google_accounts(
    db: aiosqlite.Connection,
    settings: Settings,
    token_data: dict[str, Any],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Upsert YouTube channel rows (or a drive-only fallback) with Fernet-encrypted tokens."""
    email = profile.get("email")
    user_id = profile.get("user_id")
    channels = list(profile.get("channels") or [])
    if not channels:
        fallback_ref = email or user_id or "default"
        fallback_title = (
            f"Google Drive ({email})" if email else "Google Drive (No YouTube Channel)"
        )
        channels = [
            {
                "channel_id": f"drive-only:{fallback_ref}",
                "channel_title": fallback_title,
            }
        ]
    return await _upsert_account_rows(
        db,
        settings,
        token_data,
        email=email,
        channels=channels,
        oauth_provider="google",
        default_scopes=GOOGLE_OAUTH_SCOPES,
    )


async def upsert_google_drive_account(
    db: aiosqlite.Connection,
    settings: Settings,
    token_data: dict[str, Any],
    userinfo: dict[str, Any],
) -> dict[str, Any]:
    """Upsert a dedicated 5 TB Google Drive account row with oauth_provider='google_drive'."""
    email = userinfo.get("email")
    user_id = userinfo.get("user_id")
    ref = email or user_id or "default"
    title = f"Google Drive ({email})" if email else "Google Drive"
    rows = await _upsert_account_rows(
        db,
        settings,
        token_data,
        email=email,
        channels=[{"channel_id": f"drive-only:{ref}", "channel_title": title}],
        oauth_provider="google_drive",
        default_scopes=GOOGLE_DRIVE_OAUTH_SCOPES,
    )
    return rows[0]


async def get_valid_access_token(
    db: aiosqlite.Connection,
    settings: Settings,
    account_id: int,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Return a decrypted Google access token, refreshing and persisting it if expiring."""
    async with db.execute(
        "SELECT * FROM youtube_accounts WHERE id = ?",
        (account_id,),
    ) as cur:
        account = await cur.fetchone()

    if account is None:
        raise GoogleOAuthError(f"YouTube account {account_id} not found")

    enc_key = settings.token_encryption_key
    expires_dt = _parse_iso_utc(account["token_expires_at"])
    encrypted_access = account["encrypted_access_token"]

    if (
        encrypted_access
        and expires_dt is not None
        and expires_dt > _utcnow() + timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS)
    ):
        return decrypt_token(encrypted_access, key=enc_key)

    encrypted_refresh = account["encrypted_refresh_token"]
    if not encrypted_refresh:
        raise GoogleOAuthError(
            f"YouTube account {account_id} does not have a refresh token"
        )

    oauth_provider = (
        account["oauth_provider"]
        if "oauth_provider" in account.keys()
        else None
    ) or "google"
    if oauth_provider == "google_drive":
        refresh_client_id = settings.google_drive_client_id
        refresh_client_secret = settings.google_drive_client_secret
    else:
        refresh_client_id = settings.google_client_id
        refresh_client_secret = settings.google_client_secret

    refreshed = await refresh_access_token(
        settings,
        encrypted_refresh,
        settings_key=enc_key,
        http_client=http_client,
        client_id=refresh_client_id,
        client_secret=refresh_client_secret,
    )
    new_access_token = refreshed["access_token"]
    new_encrypted_access = encrypt_token(new_access_token, key=enc_key)
    new_expires_at = refreshed.get("expires_at") or compute_token_expires_at(
        refreshed.get("expires_in", 3600)
    )
    new_scopes = refreshed.get("scope") or account["scopes"]
    now = _utcnow().isoformat()

    await db.execute(
        """
        UPDATE youtube_accounts
        SET encrypted_access_token = ?,
            token_expires_at = ?,
            scopes = ?,
            status = 'active',
            updated_at = ?
        WHERE id = ?
        """,
        (new_encrypted_access, new_expires_at, new_scopes, now, account_id),
    )
    await db.commit()
    return new_access_token
