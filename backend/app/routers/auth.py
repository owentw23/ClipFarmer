from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
import httpx

from app.config import Settings, get_settings
from app.db import get_db, utcnow_iso
from app.services.google_oauth import (
    GoogleOAuthError,
    build_google_auth_url,
    build_google_drive_auth_url,
    exchange_code_for_tokens,
    exchange_drive_code_for_tokens,
    fetch_google_user_and_channels,
    fetch_google_userinfo,
    upsert_google_accounts,
    upsert_google_drive_account,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

OAUTH_STATE_TTL_MINUTES = 10


async def get_oauth_http_client() -> AsyncIterator[httpx.AsyncClient | None]:
    """Dependency hook allowing tests to inject a MockTransport-backed AsyncClient."""
    yield None


def _parse_iso_utc(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _issue_oauth_state(db: aiosqlite.Connection, provider: str) -> str:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=OAUTH_STATE_TTL_MINUTES)).isoformat()
    state = secrets.token_urlsafe(32)

    await db.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (now.isoformat(),))
    await db.execute(
        """
        INSERT INTO oauth_states (state, provider, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (state, provider, now.isoformat(), expires_at),
    )
    await db.commit()
    return state


async def _consume_oauth_state(
    db: aiosqlite.Connection,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
    provider: str,
) -> str:
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google OAuth authorization error: {error}",
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OAuth callback parameters: code and state",
        )

    async with db.execute(
        "SELECT state, provider, expires_at FROM oauth_states WHERE state = ? AND provider = ?",
        (state, provider),
    ) as cur:
        state_row = await cur.fetchone()

    if state_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or already consumed OAuth state",
        )

    await db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    await db.commit()

    if _parse_iso_utc(state_row["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state has expired",
        )
    return code


@router.get("/google/start")
async def start_google_oauth(
    settings: Settings = Depends(get_settings),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Generate a CSRF state token and return the Google OAuth consent URL."""
    if not settings.google_client_id.strip() or not settings.google_client_secret.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google OAuth is not configured: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required",
        )

    state = await _issue_oauth_state(db, "google")
    return {
        "auth_url": build_google_auth_url(settings, state),
        "state": state,
        "configured": True,
    }


@router.get("/google/callback")
async def google_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    response_format: str | None = Query(default=None, alias="format"),
    settings: Settings = Depends(get_settings),
    db: aiosqlite.Connection = Depends(get_db),
    http_client: httpx.AsyncClient | None = Depends(get_oauth_http_client),
):
    """Validate CSRF state, exchange code for tokens, and upsert connected YouTube accounts."""
    auth_code = await _consume_oauth_state(
        db, code=code, state=state, error=error, provider="google"
    )

    if not settings.google_client_id.strip() or not settings.google_client_secret.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google OAuth is not configured: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are required",
        )

    try:
        token_data = await exchange_code_for_tokens(
            settings, auth_code, http_client=http_client
        )
        profile = await fetch_google_user_and_channels(
            token_data["access_token"],
            http_client=http_client,
        )
        accounts = await upsert_google_accounts(db, settings, token_data, profile)
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    if response_format == "json":
        return {"ok": True, "accounts": accounts}

    frontend_base = settings.frontend_base_url.rstrip("/")
    redirect_url = f"{frontend_base}/accounts?connected=youtube&count={len(accounts)}"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/google-drive/start")
async def start_google_drive_oauth(
    settings: Settings = Depends(get_settings),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Generate a CSRF state token and return the dedicated Google Drive OAuth consent URL."""
    if (
        not settings.google_drive_client_id.strip()
        or not settings.google_drive_client_secret.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Google Drive OAuth is not configured: "
                "GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET are required"
            ),
        )

    state = await _issue_oauth_state(db, "google_drive")
    return {
        "auth_url": build_google_drive_auth_url(settings, state),
        "state": state,
        "configured": True,
    }


@router.get("/google-drive/callback")
async def google_drive_oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    response_format: str | None = Query(default=None, alias="format"),
    settings: Settings = Depends(get_settings),
    db: aiosqlite.Connection = Depends(get_db),
    http_client: httpx.AsyncClient | None = Depends(get_oauth_http_client),
):
    """Validate CSRF state, exchange code, and upsert dedicated 5 TB Google Drive account."""
    auth_code = await _consume_oauth_state(
        db, code=code, state=state, error=error, provider="google_drive"
    )

    if (
        not settings.google_drive_client_id.strip()
        or not settings.google_drive_client_secret.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Google Drive OAuth is not configured: "
                "GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET are required"
            ),
        )

    try:
        token_data = await exchange_drive_code_for_tokens(
            settings, auth_code, http_client=http_client
        )
        userinfo = await fetch_google_userinfo(
            token_data["access_token"],
            http_client=http_client,
        )
        account = await upsert_google_drive_account(db, settings, token_data, userinfo)
    except GoogleOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    if response_format == "json":
        return {"ok": True, "accounts": [account]}

    frontend_base = settings.frontend_base_url.rstrip("/")
    redirect_url = f"{frontend_base}/accounts?connected=google_drive&count=1"
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
