"""Steer-marker helpers for compressed history (sibling of ``agent.conversation_compression``).

Owns the stable steer marker fallbacks, the delivery-time priority trailer, the message-text
extraction the steer detectors share, and the anchor extraction used by compression and by
display projections (web router + TUI history).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or part.get("content") or "") for part in content if isinstance(part, dict)
        )
    return ""


_STEER_FALLBACK_OPEN = "[OUT-OF-BAND USER MESSAGE"
_STEER_FALLBACK_CLOSE = "[/OUT-OF-BAND USER MESSAGE]"
_STEER_FALLBACK_TRAILER = (
    "If the user is asking for a response, address the user first. "
    "After replying, complete any unfinished tasks from previous turns."
)


def _steer_markers() -> tuple[str, str]:
    """``(open, close)`` steer markers from prompt_builder, or the stable fallback literals."""
    try:
        from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN
        return STEER_MARKER_OPEN, STEER_MARKER_CLOSE
    except Exception as exc:
        logger.debug("steer marker import failed: %s", exc, exc_info=True)
        return _STEER_FALLBACK_OPEN, _STEER_FALLBACK_CLOSE


def _steer_priority_note() -> str:
    """The delivery-time priority trailer from prompt_builder, or the stable fallback literal."""
    try:
        from agent.prompt_builder import STEER_PRIORITY_NOTE
        return STEER_PRIORITY_NOTE
    except Exception as exc:
        logger.debug("steer priority note import failed: %s", exc, exc_info=True)
        return _STEER_FALLBACK_TRAILER


def _message_contains_busy_steer(message: Any) -> bool:
    """Return whether *message* carries a busy-steer marker.
    Steer follow-ups are now their own ``role=user`` rows (caught by ``_is_real_user_message``); in
    transcripts persisted before that they ride inside ``role=tool`` results, so those still count."""
    text = _message_text(message)
    if not text:
        return False
    open_marker, close_marker = _steer_markers()
    return open_marker in text and close_marker in text


def _extract_steer_text_from_message(message: Any) -> Optional[str]:
    """Extract the inner user text from a steer marker, or None."""
    text = _message_text(message)
    if not text:
        return None
    open_marker, close_marker = _steer_markers()
    start = text.find(open_marker)
    if start == -1:
        # Fallback: marker wording may evolve; look for the stable prefix, then skip to the end of the opening line.
        start = text.find(_STEER_FALLBACK_OPEN)
        if start == -1:
            return None
        nl = text.find("\n", start)
        start = nl + 1 if nl != -1 else start + len(_STEER_FALLBACK_OPEN)
    else:
        start += len(open_marker)
    end = text.find(close_marker, start)
    if end == -1:
        end = text.find(_STEER_FALLBACK_CLOSE, start)
        if end == -1:
            return None
    inner = text[start:end].strip()
    # The delivery-time priority trailer is meta-guidance, not the user's words
    # (see prompt_builder.STEER_PRIORITY_NOTE): strip it before the text becomes an anchor
    # or a display projection.
    trailer = _steer_priority_note()
    if trailer and inner.endswith(trailer):
        inner = inner[: -len(trailer)].rstrip()
    return inner or None


def _compressed_has_busy_steer(messages: list) -> bool:
    """Whether *messages* already carries a steer marker in a ``role=tool`` row.
    Only tool rows count, so a summary merely quoting the marker text is not mistaken for live intent."""
    return any(
        isinstance(msg, dict) and msg.get("role") == "tool" and _message_contains_busy_steer(msg) for msg in messages
    )
