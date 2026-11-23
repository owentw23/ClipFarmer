from __future__ import annotations

import asyncio
import subprocess
from typing import Any


class _ThreadProcess:
    """Async wrapper around subprocess.Popen for Windows SelectorEventLoop compatibility."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.stdin = proc.stdin
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.poll()

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        out, err = await asyncio.to_thread(self._proc.communicate, input)
        return out or b"", err or b""

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)

    def send_signal(self, sig: int) -> None:
        self._proc.send_signal(sig)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()


def _map_stdio(value: Any) -> Any:
    if value == asyncio.subprocess.PIPE:
        return subprocess.PIPE
    if value == asyncio.subprocess.DEVNULL:
        return subprocess.DEVNULL
    if value == asyncio.subprocess.STDOUT:
        return subprocess.STDOUT
    return value


async def create_subprocess_exec(*cmd: str, **kwargs: Any) -> Any:
    """Spawn a subprocess cleanly across both ProactorEventLoop and SelectorEventLoop."""
    try:
        return await asyncio.create_subprocess_exec(*cmd, **kwargs)
    except NotImplementedError:
        mapped = dict(kwargs)
        for key in ("stdin", "stdout", "stderr"):
            if key in mapped:
                mapped[key] = _map_stdio(mapped[key])
        proc = subprocess.Popen(list(cmd), **mapped)
        return _ThreadProcess(proc)
