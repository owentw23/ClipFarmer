from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_LLM_MODEL = "deepseek-v4-1-flash-economy-261017"
CODING_PLAN_PATH_FRAGMENT = "/api/coding/v3"
MAX_TITLE_LENGTH = 100
MAX_TAGS = 15

_SYSTEM_PROMPT = (
    "You write viral YouTube Shorts metadata for gaming/livestream highlight clips. "
    "Return ONLY valid JSON matching this exact schema: "
    '{"title": "<punchy YouTube Shorts title <= 95 chars>", '
    '"description": "<STRICTLY ONE concise sentence for YouTube Shorts>", '
    '"tags": ["tag1", "tag2"]}. '
    "No markdown, no commentary, no extra keys."
)


def enforce_one_sentence(text: str) -> str:
    """Collapse text to a single line and keep strictly the first sentence."""
    normalized = re.sub(r"\s+", " ", (text or "")).strip()
    if not normalized:
        return ""

    # Stop at the first terminal punctuation followed by whitespace or end-of-string,
    # avoiding splits inside decimals/versions like 3.5 or v1.2.
    match = re.match(r"^(.+?[.!?]+[\"'\)\]]*)(?:\s+|$)", normalized)
    sentence = match.group(1).strip() if match else normalized

    if sentence[-1] in ",;:-":
        sentence = sentence.rstrip(",;:-").strip()
    if not sentence:
        return ""

    if sentence[-1] in "\"')]}":
        if len(sentence) < 2 or sentence[-2] not in ".!?":
            sentence = f"{sentence}."
    elif sentence[-1] not in ".!?":
        sentence = f"{sentence}."

    return sentence


def _clean_single_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _truncate_title(title: str, max_len: int = MAX_TITLE_LENGTH) -> str:
    cleaned = _clean_single_line(title)
    if len(cleaned) <= max_len:
        return cleaned
    clipped = cleaned[:max_len].rstrip()
    if " " in clipped:
        word_safe = clipped.rsplit(" ", 1)[0].rstrip(" ,;:-")
        if len(word_safe) >= max_len // 2:
            return word_safe
    return clipped


def _normalize_tags(raw_tags: Any, fallback_tags: list[str]) -> list[str]:
    if isinstance(raw_tags, str):
        candidates = [part.strip() for part in raw_tags.split(",")]
    elif isinstance(raw_tags, list):
        candidates = [str(item).strip() for item in raw_tags if item is not None]
    else:
        candidates = []

    seen: set[str] = set()
    cleaned: list[str] = []
    for item in candidates:
        tag = _clean_single_line(item).lstrip("#").strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(tag[:40])
        if len(cleaned) >= MAX_TAGS:
            break

    return cleaned or fallback_tags


def _strip_code_fences(raw_text: str) -> str:
    stripped = (raw_text or "").strip()
    fence_match = re.match(
        r"^```(?:json)?\s*\r?\n?(.*?)\r?\n?```\s*$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def _extract_choice_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("LLM response missing message")
    content = message.get("content")
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        content = "".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response content is empty")
    return content


class CopywriterService:
    """Generates YouTube Shorts title, 1-sentence description, and tags via an LLM."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._http_client = http_client

    def _build_fallback(
        self,
        *,
        transcript: str = "",
        neon_title: str = "",
        neon_hook: str = "",
        streamer_name: str = "",
        stream_title: str = "",
        platform: str = "",
    ) -> dict[str, Any]:
        clean_neon_title = _clean_single_line(neon_title)
        clean_neon_hook = _clean_single_line(neon_hook)
        clean_streamer = _clean_single_line(streamer_name)
        clean_stream_title = _clean_single_line(stream_title)
        clean_platform = _clean_single_line(platform)

        if clean_neon_title:
            raw_title = clean_neon_title
        elif clean_neon_hook:
            raw_title = clean_neon_hook
        elif clean_streamer and clean_stream_title:
            raw_title = f"{clean_streamer} - {clean_stream_title}"
        elif clean_streamer:
            raw_title = f"{clean_streamer} Highlight"
        elif clean_stream_title:
            raw_title = clean_stream_title
        else:
            raw_title = "Stream Highlight"

        title = _truncate_title(raw_title) or "Stream Highlight"

        lead = (
            f"{clean_streamer} ({clean_stream_title})"
            if (clean_streamer and clean_stream_title)
            else (clean_streamer or clean_stream_title)
        )
        detail = enforce_one_sentence(clean_neon_hook or clean_neon_title)
        if lead and detail:
            candidate_desc = f"{lead} - {detail}"
        elif detail:
            candidate_desc = detail
        elif lead:
            candidate_desc = f"Highlight clip from {lead}."
        elif _clean_single_line(transcript):
            candidate_desc = transcript
        else:
            candidate_desc = "Stream highlight clip."

        description = enforce_one_sentence(candidate_desc) or "Stream highlight clip."
        fallback_tags = _normalize_tags(
            [clean_streamer, clean_platform, "Shorts", "Clips"],
            fallback_tags=["Shorts", "Clips"],
        )
        return {
            "title": title,
            "description": description,
            "tags": fallback_tags,
            "source": "fallback",
        }

    def _build_user_prompt(
        self,
        *,
        transcript: str,
        neon_title: str,
        neon_hook: str,
        streamer_name: str,
        stream_title: str,
        platform: str,
        visual_summary: str = "",
    ) -> str:
        lines = [
            f"Streamer: {streamer_name or 'Unknown'}",
            f"Platform: {platform or 'livestream'}",
            f"Stream Title: {stream_title or 'N/A'}",
            f"Suggested Title: {neon_title or 'N/A'}",
            f"Hook: {neon_hook or 'N/A'}",
        ]
        clean_visual = (visual_summary or "").strip()
        if clean_visual:
            lines.append(f"Visual Context: {clean_visual}")
        lines.append(f"Transcript:\n{(transcript or '').strip()[:3000] or 'N/A'}")
        return "\n".join(lines)

    async def _post_chat_completions(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(endpoint, headers=headers, json=payload)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(endpoint, headers=headers, json=payload)

    async def generate_youtube_copy(
        self,
        *,
        transcript: str,
        neon_title: str = "",
        neon_hook: str = "",
        streamer_name: str = "",
        stream_title: str = "",
        platform: str = "",
        visual_summary: str = "",
    ) -> dict[str, Any]:
        """Generate YouTube Shorts title, strictly 1-sentence description, and tags."""
        fallback = self._build_fallback(
            transcript=transcript,
            neon_title=neon_title,
            neon_hook=neon_hook,
            streamer_name=streamer_name,
            stream_title=stream_title,
            platform=platform,
        )

        api_key = (self.settings.llm_api_key or "").strip()
        base_url = (self.settings.llm_base_url or "").strip().rstrip("/")
        if not api_key or not base_url:
            return fallback

        if CODING_PLAN_PATH_FRAGMENT in base_url.lower():
            logger.warning(
                "Blocked LLM copywriter call to Coding Plan endpoint (%s); using fallback copy.",
                base_url,
            )
            return fallback

        model = (self.settings.llm_model or "").strip() or DEFAULT_LLM_MODEL
        endpoint = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        user_prompt = self._build_user_prompt(
            transcript=transcript,
            neon_title=neon_title,
            neon_hook=neon_hook,
            streamer_name=streamer_name,
            stream_title=stream_title,
            platform=platform,
            visual_summary=visual_summary,
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 400,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }

        retry_payload = dict(payload)
        try:
            response = await self._post_chat_completions(endpoint, headers, retry_payload)
            for _ in range(2):
                if response.status_code != 400:
                    break
                err_lower = (response.text or "").lower()
                if (
                    "response_format" in retry_payload
                    and "response_format" in err_lower
                    and "thinking" not in err_lower
                ):
                    retry_payload.pop("response_format", None)
                elif "thinking" in retry_payload:
                    retry_payload.pop("thinking", None)
                elif "response_format" in retry_payload:
                    retry_payload.pop("response_format", None)
                else:
                    break
                response = await self._post_chat_completions(endpoint, headers, retry_payload)

            if response.status_code >= 400:
                logger.warning(
                    "LLM copywriter request failed with HTTP %s; using fallback copy.",
                    response.status_code,
                )
                return fallback

            raw_json = response.json()
            raw_content = _extract_choice_content(raw_json)
            parsed = json.loads(_strip_code_fences(raw_content))
            if not isinstance(parsed, dict):
                raise ValueError("LLM JSON payload must be an object")
        except Exception as exc:
            logger.warning("LLM copywriter failed (%s: %s); using fallback copy.", type(exc).__name__, exc)
            return fallback

        title = _truncate_title(str(parsed.get("title") or "")) or fallback["title"]
        description = enforce_one_sentence(str(parsed.get("description") or "")) or fallback["description"]
        tags = _normalize_tags(parsed.get("tags"), fallback_tags=fallback["tags"])

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "source": "llm",
        }
