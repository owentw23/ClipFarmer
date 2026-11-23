from __future__ import annotations

import asyncio
import html
import json
import logging
from pathlib import Path
import re
import shutil
import sys
from typing import Any

import httpx

from app.services.subprocess_compat import create_subprocess_exec

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_IS_LIVE_RE = re.compile(r'"(?:isLive|isLiveNow)"\s*:\s*true')
_CANONICAL_WATCH_RE = re.compile(
    r'<link\s+rel=["\']canonical["\']\s+href=["\']https?://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{6,})["\']',
    re.IGNORECASE,
)
_VIDEO_ID_RE = re.compile(r'"videoId"\s*:\s*"([A-Za-z0-9_-]{6,})"')
_VIDEO_DETAILS_TITLE_RE = re.compile(
    r'"videoDetails"\s*:\s*\{[^{}]*?"title"\s*:\s*("(?:\\.|[^"\\])*")',
    re.DOTALL,
)
_JSON_TITLE_RE = re.compile(r'"title"\s*:\s*("(?:\\.|[^"\\])*")')
_OG_TITLE_RE = re.compile(
    r'<meta\s+(?:property=["\']og:title["\']|name=["\']title["\'])\s+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_HTML_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _resolve_executable(name: str) -> str | None:
    """Resolve an executable inside the current Python venv first, then system PATH."""
    exe_dir = Path(sys.executable).resolve().parent
    for candidate_name in (f"{name}.exe", name):
        candidate = exe_dir / candidate_name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


class YouTubeWatcher:
    """Zero-quota YouTube /live HTML watcher with yt-dlp confirmation helper."""

    def __init__(
        self,
        cookies_file: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cookies_file = cookies_file.strip()
        self._http_client = http_client
        self.last_probe_errors: set[str] = set()

    @staticmethod
    def build_live_url(channel_ref: str) -> str:
        """Build canonical YouTube /live URL for @handles or UC channel IDs."""
        ref = channel_ref.strip().rstrip("/")
        if ref.startswith(("http://", "https://")):
            return ref if ref.endswith("/live") else f"{ref}/live"
        if ref.startswith("channel/"):
            return f"https://www.youtube.com/{ref}/live"
        if ref.startswith("@"):
            return f"https://www.youtube.com/{ref}/live"
        if ref.startswith("UC") and len(ref) >= 10:
            return f"https://www.youtube.com/channel/{ref}/live"
        return f"https://www.youtube.com/{ref}/live"

    @staticmethod
    def parse_live_html(html_text: str, fallback_url: str = "") -> dict[str, Any] | None:
        """Parse YouTube /live watch HTML for live status, canonical videoId, and title."""
        if not html_text or not _IS_LIVE_RE.search(html_text):
            return None

        video_id = ""
        canonical_match = _CANONICAL_WATCH_RE.search(html_text)
        if canonical_match:
            video_id = canonical_match.group(1)
        else:
            vid_match = _VIDEO_ID_RE.search(html_text)
            if vid_match:
                video_id = vid_match.group(1)

        title = ""
        title_match = _VIDEO_DETAILS_TITLE_RE.search(html_text) or _JSON_TITLE_RE.search(html_text)
        if title_match:
            try:
                title = str(json.loads(title_match.group(1)))
            except json.JSONDecodeError:
                title = ""
        if not title:
            og_match = _OG_TITLE_RE.search(html_text)
            if og_match:
                title = html.unescape(og_match.group(1)).strip()
        if not title:
            html_title_match = _HTML_TITLE_RE.search(html_text)
            if html_title_match:
                raw_title = html.unescape(html_title_match.group(1)).strip()
                title = re.sub(r"\s*-\s*YouTube$", "", raw_title).strip()

        stream_url = (
            f"https://www.youtube.com/watch?v={video_id}"
            if video_id
            else fallback_url
        )
        return {
            "external_stream_id": video_id,
            "title": title,
            "stream_url": stream_url,
        }

    async def check_channel(
        self,
        channel_ref: str,
        *,
        confirm_ytdlp: bool = False,
    ) -> dict[str, Any] | None:
        """Probe YouTube /live URL with zero API quota; returns stream info dict or None."""
        live_url = self.build_live_url(channel_ref)
        self.last_probe_errors.discard(channel_ref)

        async def _fetch(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                live_url,
                headers=DEFAULT_HEADERS,
                follow_redirects=True,
            )

        try:
            if self._http_client is not None:
                resp = await _fetch(self._http_client)
            else:
                async with httpx.AsyncClient(timeout=15.0) as temp_client:
                    resp = await _fetch(temp_client)
            resp.raise_for_status()
        except Exception as exc:
            self.last_probe_errors.add(channel_ref)
            logger.warning("YouTube /live probe failed for %s: %s", channel_ref, exc)
            return None

        parsed = self.parse_live_html(resp.text, fallback_url=str(resp.url or live_url))
        if parsed is None:
            return None

        if not parsed["external_stream_id"] and resp.url:
            url_str = str(resp.url)
            vid_q = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", url_str)
            if vid_q:
                parsed["external_stream_id"] = vid_q.group(1)
                parsed["stream_url"] = f"https://www.youtube.com/watch?v={vid_q.group(1)}"

        if confirm_ytdlp:
            confirmed = await self.confirm_with_ytdlp(parsed["stream_url"])
            if not confirmed.get("is_live"):
                return None
            if confirmed.get("id"):
                parsed["external_stream_id"] = confirmed["id"]
                parsed["stream_url"] = f"https://www.youtube.com/watch?v={confirmed['id']}"
            if confirmed.get("title"):
                parsed["title"] = confirmed["title"]

        return parsed

    async def confirm_with_ytdlp(
        self,
        video_url: str,
        cookies_file: str = "",
    ) -> dict[str, Any]:
        """Run yt-dlp --dump-single-json --no-playlist to confirm is_live and retrieve title/id."""
        ytdlp_exe = _resolve_executable("yt-dlp")
        if not ytdlp_exe:
            logger.warning("yt-dlp executable not found")
            return {"is_live": False, "id": "", "title": "", "stream_url": video_url}

        cmd: list[str] = [ytdlp_exe, "--dump-single-json", "--no-playlist"]

        node_path = shutil.which("node")
        if node_path:
            cmd.extend(["--js-runtimes", f"node:{node_path}"])

        effective_cookies = (cookies_file or self.cookies_file).strip()
        if effective_cookies and Path(effective_cookies).is_file():
            cmd.extend(["--cookies", effective_cookies])

        cmd.append(video_url)

        try:
            proc = await create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("yt-dlp --dump-single-json timed out for %s", video_url)
            return {"is_live": False, "id": "", "title": "", "stream_url": video_url}
        except Exception as exc:
            logger.warning("yt-dlp --dump-single-json failed for %s: %s", video_url, exc)
            return {"is_live": False, "id": "", "title": "", "stream_url": video_url}

        if proc.returncode != 0 or not stdout_bytes:
            return {"is_live": False, "id": "", "title": "", "stream_url": video_url}

        try:
            data = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {"is_live": False, "id": "", "title": "", "stream_url": video_url}

        is_live = bool(data.get("is_live")) or data.get("live_status") == "is_live"
        return {
            "is_live": is_live,
            "id": str(data.get("id") or ""),
            "title": str(data.get("title") or ""),
            "stream_url": str(data.get("webpage_url") or video_url),
        }
