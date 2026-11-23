"""Non-fatal live chat capture for Twitch (anonymous IRC) and YouTube (optional pytchat)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import random
import re
import socket
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6667

_PRIVMSG_RE = re.compile(
    r"^(?:@\S+\s+)?:([^!\s]+)!\S+\s+PRIVMSG\s+\S+\s+:(.*)$"
)
_PING_RE = re.compile(r"^PING(?:\s+(.*))?$")


def parse_twitch_irc_line(raw_line: str) -> tuple[str, str] | None:
    """Parse a raw Twitch IRC line; return (nick, message) for PRIVMSG or None."""
    line = (raw_line or "").rstrip("\r\n")
    if not line:
        return None
    match = _PRIVMSG_RE.match(line)
    if not match:
        return None
    nick = match.group(1).strip()
    text = match.group(2)
    if not nick or not text:
        return None
    return nick, text


def format_twitch_pong(ping_line: str) -> str | None:
    """Return the PONG response string if ping_line is an IRC PING, else None."""
    line = (ping_line or "").rstrip("\r\n")
    match = _PING_RE.match(line)
    if not match:
        return None
    payload = (match.group(1) or ":tmi.twitch.tv").strip()
    return f"PONG {payload}\r\n"


def _extract_twitch_channel(channel_ref: str, stream_url: str = "") -> str:
    candidate = (channel_ref or "").strip().lstrip("#@")
    if "/" in candidate:
        parsed = urlparse(candidate)
        parts = [p for p in parsed.path.split("/") if p]
        candidate = parts[-1] if parts else ""
    if not candidate and stream_url:
        parsed = urlparse(stream_url)
        parts = [p for p in parsed.path.split("/") if p]
        candidate = parts[-1] if parts else ""
    return candidate.strip().lower()


def _extract_youtube_video_id(external_stream_id: str = "", stream_url: str = "", channel_ref: str = "") -> str:
    ext = (external_stream_id or "").strip()
    if ext and "/" not in ext and "?" not in ext:
        return ext
    for raw in (stream_url, ext, channel_ref):
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.hostname and "youtu.be" in parsed.hostname:
            vid = parsed.path.strip("/").split("/")[0]
            if vid:
                return vid
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            return qs["v"][0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"live", "shorts", "embed"}:
            return parts[1]
    return ""


class LiveChatRecorder:
    """Background daemon chat recorder writing chunk-relative JSONL events."""

    def __init__(
        self,
        platform: str,
        channel_ref: str,
        chat_path: str | Path,
        *,
        chunk_start_monotonic: float | None = None,
        external_stream_id: str = "",
        stream_url: str = "",
        irc_host: str = TWITCH_IRC_HOST,
        irc_port: int = TWITCH_IRC_PORT,
    ) -> None:
        self.platform = (platform or "").strip().lower()
        self.channel_ref = (channel_ref or "").strip()
        self.external_stream_id = (external_stream_id or "").strip()
        self.stream_url = (stream_url or "").strip()
        self.irc_host = irc_host
        self.irc_port = int(irc_port)

        self._lock = threading.Lock()
        self._chat_path = Path(chat_path)
        self._chunk_start_monotonic = (
            float(chunk_start_monotonic)
            if chunk_start_monotonic is not None
            else time.monotonic()
        )
        self._fh: Any = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_sock: socket.socket | None = None
        self._active_pytchat: Any = None

    @property
    def chat_path(self) -> Path:
        with self._lock:
            return self._chat_path

    def start(self) -> None:
        """Start background daemon chat capture thread (non-fatal)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        if self.platform not in {"twitch", "youtube"}:
            logger.debug("LiveChatRecorder unsupported platform %r; skipping", self.platform)
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"chat-recorder-{self.platform}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 0.5) -> None:
        """Stop background chat capture cleanly and close open handles."""
        self._stop_event.set()
        sock = self._active_sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        chat = self._active_pytchat
        if chat is not None:
            try:
                chat.terminate()
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(0.05, float(timeout)))
        self._close_file()

    def rotate_chunk(
        self,
        new_chat_path: str | Path,
        chunk_start_monotonic: float | None = None,
    ) -> None:
        """Switch output to a new chunk JSONL file and reset relative timestamps to 0.0."""
        with self._lock:
            self._close_file_locked()
            self._chat_path = Path(new_chat_path)
            self._chunk_start_monotonic = (
                float(chunk_start_monotonic)
                if chunk_start_monotonic is not None
                else time.monotonic()
            )

    def _close_file(self) -> None:
        with self._lock:
            self._close_file_locked()

    def _close_file_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            finally:
                self._fh = None

    def _write_message(
        self,
        author: str,
        text: str,
        kind: str = "message",
        *,
        monotonic_ts: float | None = None,
    ) -> None:
        now_m = float(monotonic_ts) if monotonic_ts is not None else time.monotonic()
        with self._lock:
            t_rel = max(0.0, round(now_m - self._chunk_start_monotonic, 2))
            if self._fh is None:
                self._chat_path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self._chat_path, "a", encoding="utf-8")
            payload = {
                "t": t_rel,
                "author": str(author),
                "text": str(text),
                "kind": str(kind),
            }
            self._fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._fh.flush()

    def handle_irc_line(self, raw_line: str, sock: Any | None = None) -> bool:
        """Process one Twitch IRC line (PING -> PONG, PRIVMSG -> JSONL). Returns True if handled."""
        pong = format_twitch_pong(raw_line)
        if pong is not None:
            if sock is not None:
                try:
                    sock.sendall(pong.encode("utf-8"))
                except OSError:
                    pass
            return True
        parsed = parse_twitch_irc_line(raw_line)
        if parsed is not None:
            nick, msg = parsed
            self._write_message(nick, msg, "message")
            return True
        return False

    def _run_loop(self) -> None:
        try:
            if self.platform == "twitch":
                self._run_twitch_irc()
            elif self.platform == "youtube":
                self._run_youtube_chat()
        except Exception as exc:
            logger.debug("LiveChatRecorder exited (%s): %s", self.platform, exc)
        finally:
            self._close_file()

    def _run_twitch_irc(self) -> None:
        channel = _extract_twitch_channel(self.channel_ref, self.stream_url)
        if not channel:
            logger.debug("LiveChatRecorder: empty Twitch channel; skipping")
            return

        backoff = 1.0
        while not self._stop_event.is_set():
            sock: socket.socket | None = None
            try:
                sock = socket.create_connection((self.irc_host, self.irc_port), timeout=10.0)
                sock.settimeout(3.0)
                self._active_sock = sock
                nick = f"justinfan{random.randint(10000, 99999)}"
                handshake = (
                    "PASS SCHMOOPIIE\r\n"
                    f"NICK {nick}\r\n"
                    f"JOIN #{channel}\r\n"
                )
                sock.sendall(handshake.encode("utf-8"))
                backoff = 1.0

                buf = ""
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        self.handle_irc_line(line, sock=sock)
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.debug("Twitch IRC chat capture reconnect (%s): %s", channel, exc)
            finally:
                self._active_sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if self._stop_event.is_set():
                break
            self._stop_event.wait(backoff)
            backoff = min(30.0, backoff * 2.0)

    def _run_youtube_chat(self) -> None:
        try:
            import pytchat  # type: ignore[import-not-found]
        except ImportError:
            logger.debug("pytchat not installed; YouTube chat capture disabled")
            return

        video_id = _extract_youtube_video_id(
            self.external_stream_id, self.stream_url, self.channel_ref
        )
        if not video_id:
            logger.debug("LiveChatRecorder: missing YouTube video_id; skipping")
            return

        backoff = 2.0
        while not self._stop_event.is_set():
            chat = None
            try:
                chat = pytchat.create(video_id=video_id, interruptable=False)
                self._active_pytchat = chat
                backoff = 2.0
                while not self._stop_event.is_set() and chat.is_alive():
                    for item in chat.get().sync_items():
                        author = getattr(getattr(item, "author", None), "name", "") or "viewer"
                        msg = getattr(item, "message", "") or ""
                        if msg:
                            self._write_message(author, msg, "message")
                    self._stop_event.wait(0.5)
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.debug("YouTube chat capture reconnect (%s): %s", video_id, exc)
            finally:
                self._active_pytchat = None
                if chat is not None:
                    try:
                        chat.terminate()
                    except Exception:
                        pass

            if self._stop_event.is_set():
                break
            self._stop_event.wait(backoff)
            backoff = min(30.0, backoff * 2.0)
