"""Repair text that was decoded with the wrong codepage.

Stream titles arrive as UTF-8, but on Windows a subprocess pipe or a mislabelled page can
be read as cp1252, turning "🚘 DAY 4" into "ðŸš˜ DAY 4" (with lone surrogates standing in
for bytes cp1252 has no character for). Reversing that is lossless.
"""
from __future__ import annotations

_MARKERS = ("Ã", "Â", "ð", "â€", "â")


def _has_escaped_bytes(text: str) -> bool:
    return any(0xDC80 <= ord(ch) <= 0xDCFF for ch in text)


def repair_text(value: str | None) -> str:
    """Return value with cp1252/latin-1 mojibake undone; unchanged when it is already clean."""
    if not value:
        return value or ""
    text = str(value)
    if not (_has_escaped_bytes(text) or any(m in text for m in _MARKERS)):
        return text
    for codec in ("cp1252", "latin-1"):
        try:
            return text.encode(codec, errors="surrogateescape").decode("utf-8")
        except UnicodeError:
            continue
    # Not reversible: at least drop the lone surrogates so the text can be stored and sent.
    return text.encode("utf-8", errors="replace").decode("utf-8")
