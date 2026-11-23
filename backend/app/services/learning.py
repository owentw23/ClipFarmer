"""Teach NeonClipping's ranking from what happens to its drafts.

Each draft keeps the scoring features the engine ranked it by (clip_drafts.features_json).
Its fate becomes a 1-5 rating for NeonClipping's learn.py:

  rejected (kept local or archived) .......... 2
  posted ..................................... 4
  posted, then in the top third by engaged views among posted clips with analytics ... 5
  posted, then in the bottom third ........... 3

The loop compares the rating each draft should have with the one it last sent and records
only the changes, through engines/neonclipping/learn_cli.py in the engine's own Python.
learn.py refits weights.json in the engine's data folder (NEONCLIPPING_DATA_DIR), which
pipeline_runner.py loads for every chunk. It adapts after 8 rated drafts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import open_connection, utcnow_iso

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
REJECTED = {"kept_local", "archived"}
MIN_FOR_PERFORMANCE = 3


def desired_ratings(rows: list[dict[str, Any]]) -> dict[int, tuple[int, str]]:
    """Map clip id -> (rating, source) from status and, when available, performance."""
    out: dict[int, tuple[int, str]] = {}
    measured = sorted(
        (r for r in rows if r["status"] == "posted" and r.get("engaged_views") is not None),
        key=lambda r: float(r["engaged_views"] or 0),
    )
    tiers: dict[int, int] = {}
    if len(measured) >= MIN_FOR_PERFORMANCE:
        n = len(measured)
        for i, r in enumerate(measured):
            tiers[int(r["id"])] = 3 if i < n / 3 else (5 if i >= 2 * n / 3 else 4)
    for r in rows:
        cid = int(r["id"])
        if r["status"] in REJECTED:
            out[cid] = (2, "rejected")
        elif r["status"] == "posted":
            if cid in tiers:
                out[cid] = (tiers[cid], "performance")
            else:
                out[cid] = (4, "posted")
    return out


class LearningService:
    def __init__(self, settings: Settings, engine_dir: Path | None = None, python: Path | None = None) -> None:
        self.settings = settings
        self.engine_dir = engine_dir or (_REPO_ROOT / "engines" / "neonclipping")
        self.python = python or (self.engine_dir / ".venv" / "Scripts" / "python.exe")
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _run_cli(self, payload: dict[str, Any]) -> dict[str, Any]:
        script = self.engine_dir / "learn_cli.py"
        if not script.is_file() or not self.python.is_file():
            raise FileNotFoundError(f"NeonClipping learning bridge not found at {script}")
        proc = subprocess.run(
            [str(self.python), str(script)],
            input=json.dumps({"data_dir": self.settings.neonclipping_data_dir, **payload}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self.engine_dir),
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"learn_cli failed: {(proc.stderr or proc.stdout)[-400:]}")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else {}

    async def summary(self) -> dict[str, Any]:
        """What the ranking has learned so far, plus how many drafts fed it."""
        result = await asyncio.to_thread(self._run_cli, {"mode": "summary"})
        conn = await open_connection(self.settings.db_path)
        try:
            async with conn.execute(
                """
                SELECT COUNT(*) AS rated,
                       SUM(CASE WHEN rating >= 4 THEN 1 ELSE 0 END) AS liked,
                       SUM(CASE WHEN rating <= 2 THEN 1 ELSE 0 END) AS rejected,
                       SUM(CASE WHEN features_json IS NOT NULL AND rating IS NULL THEN 1 ELSE 0 END) AS waiting
                FROM clip_drafts WHERE features_json IS NOT NULL
                """
            ) as cur:
                row = await cur.fetchone()
        finally:
            await conn.close()
        result["drafts"] = {k: int(row[k] or 0) for k in ("rated", "liked", "rejected", "waiting")}
        return result

    async def sync(self) -> dict[str, Any]:
        """Send every rating that changed since the last sync; returns what was recorded."""
        async with self._lock:
            conn = await open_connection(self.settings.db_path)
            try:
                async with conn.execute(
                    """
                    SELECT cd.id, cd.status, cd.features_json, cd.rating, cd.rating_source,
                           (SELECT MAX(ms.engaged_views) FROM publications p
                              JOIN metric_snapshots ms ON ms.publication_id = p.id
                             WHERE p.clip_draft_id = cd.id AND ms.analytics_synced_at IS NOT NULL
                           ) AS engaged_views
                    FROM clip_drafts cd
                    WHERE cd.features_json IS NOT NULL
                    """
                ) as cur:
                    rows = [dict(r) for r in await cur.fetchall()]
                wanted = desired_ratings(rows)
                by_id = {int(r["id"]): r for r in rows}
                changes = [
                    (cid, rating, source)
                    for cid, (rating, source) in wanted.items()
                    if by_id[cid]["rating"] != rating
                ]
                if not changes:
                    return {"recorded": 0}
                ratings = []
                for cid, rating, _ in changes:
                    try:
                        features = json.loads(by_id[cid]["features_json"])
                    except (TypeError, ValueError):
                        continue
                    ratings.append({"file": f"clip{cid}", "rating": rating, "features": features})
                result = await asyncio.to_thread(self._run_cli, {"mode": "record", "ratings": ratings})
                now = utcnow_iso()
                skipped = {s.get("file") for s in result.get("skipped") or []}
                for cid, rating, source in changes:
                    if f"clip{cid}" in skipped:
                        continue
                    await conn.execute(
                        "UPDATE clip_drafts SET rating = ?, rating_source = ?, rated_at = ? WHERE id = ?",
                        (rating, source, now, cid),
                    )
                await conn.commit()
                return {"recorded": int(result.get("recorded") or 0), "fitted": result.get("fitted")}
            finally:
                await conn.close()

    async def _loop(self) -> None:
        interval = max(30.0, float(self.settings.learning_sync_interval_sec))
        while not self._stop.is_set():
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Learning sync failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="learning-sync")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
