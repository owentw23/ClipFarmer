from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.db import get_db, utcnow_iso

router = APIRouter(prefix="/api/streamers", tags=["targets"])


class TargetItemInput(BaseModel):
    youtube_account_id: int
    default_privacy: Literal["public", "unlisted", "private"] = "public"
    enabled: bool = True


class TargetsUpdateRequest(BaseModel):
    targets: list[TargetItemInput]


def _serialize_target(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "streamer_id": row["streamer_id"],
        "youtube_account_id": row["youtube_account_id"],
        "channel_id": row["channel_id"],
        "channel_title": row["channel_title"],
        "default_privacy": row["default_privacy"],
        "enabled": bool(row["enabled"]),
    }


async def _ensure_streamer_exists(db: aiosqlite.Connection, streamer_id: int) -> None:
    async with db.execute("SELECT id FROM streamers WHERE id = ?", (streamer_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Streamer not found")


async def _fetch_targets(db: aiosqlite.Connection, streamer_id: int) -> list[dict[str, Any]]:
    async with db.execute(
        """
        SELECT st.id, st.streamer_id, st.youtube_account_id,
               ya.channel_id, ya.channel_title,
               st.default_privacy, st.enabled
        FROM streamer_targets st
        JOIN youtube_accounts ya ON ya.id = st.youtube_account_id
        WHERE st.streamer_id = ?
        ORDER BY st.id ASC
        """,
        (streamer_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_serialize_target(row) for row in rows]


@router.get("/{streamer_id}/targets")
async def get_streamer_targets(
    streamer_id: int,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """List YouTube account publishing targets configured for a streamer."""
    await _ensure_streamer_exists(db, streamer_id)
    return {
        "streamer_id": streamer_id,
        "items": await _fetch_targets(db, streamer_id),
    }


@router.put("/{streamer_id}/targets")
async def replace_streamer_targets(
    streamer_id: int,
    payload: TargetsUpdateRequest,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Atomically replace YouTube account targets for a streamer."""
    await _ensure_streamer_exists(db, streamer_id)

    account_ids = [item.youtube_account_id for item in payload.targets]
    if len(account_ids) != len(set(account_ids)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate youtube_account_id in targets",
        )

    if account_ids:
        placeholders = ", ".join("?" for _ in account_ids)
        async with db.execute(
            f"SELECT id FROM youtube_accounts WHERE id IN ({placeholders})",
            account_ids,
        ) as cur:
            found_rows = await cur.fetchall()
        found_ids = {int(row["id"]) for row in found_rows}
        missing_ids = [aid for aid in account_ids if aid not in found_ids]
        if missing_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown youtube_account_id(s): {missing_ids}",
            )

    now = utcnow_iso()
    await db.execute("DELETE FROM streamer_targets WHERE streamer_id = ?", (streamer_id,))
    for item in payload.targets:
        await db.execute(
            """
            INSERT INTO streamer_targets (
                streamer_id, youtube_account_id, default_privacy,
                default_tags_json, enabled, created_at
            )
            VALUES (?, ?, ?, '[]', ?, ?)
            """,
            (
                streamer_id,
                item.youtube_account_id,
                item.default_privacy,
                1 if item.enabled else 0,
                now,
            ),
        )
    await db.commit()

    return {
        "streamer_id": streamer_id,
        "items": await _fetch_targets(db, streamer_id),
    }
