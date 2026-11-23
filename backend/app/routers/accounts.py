from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status

from app.db import get_db
from app.services.google_oauth import serialize_youtube_account

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("")
async def list_accounts(db: aiosqlite.Connection = Depends(get_db)) -> dict[str, Any]:
    """List connected YouTube accounts without exposing encrypted tokens."""
    async with db.execute(
        """
        SELECT id, channel_id, channel_title, email, oauth_provider, encrypted_refresh_token,
               scopes, status, token_expires_at, created_at, updated_at
        FROM youtube_accounts
        ORDER BY id ASC
        """
    ) as cur:
        rows = await cur.fetchall()
    return {"items": [serialize_youtube_account(row) for row in rows]}


@router.delete("/{account_id}")
async def delete_account(
    account_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Disconnect and delete a YouTube account by ID."""
    async with db.execute(
        "SELECT id FROM youtube_accounts WHERE id = ?",
        (account_id,),
    ) as cur:
        existing = await cur.fetchone()
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found",
        )

    await db.execute("DELETE FROM youtube_accounts WHERE id = ?", (account_id,))
    await db.commit()
    return {"ok": True, "id": account_id}
