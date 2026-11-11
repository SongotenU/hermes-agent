"""Magic keywords — oh-my-pi parity (``docs/magic-keywords.md``).

A user typing a designated word anywhere in their prompt arms one extra,
hidden instruction for THAT turn only. The word stays in the visible message;
the notice is appended to the same user-attributed row (never a system-prompt
mutation, never a synthetic mid-loop message), so prompt caching and role
alternation are untouched — the notice is part of the CURRENT turn's user
message, which is exactly the sanctioned injection surface.

Matching rules (ported design, not code):
- Case-insensitive, whole-word.
- Never matches inside a fenced code block (```` ``` ```` or ``~~~``) — quoting a
  keyword in code is not intent.
- Idempotent: a notice already present (resent/edited message, retry) is never
  duplicated.
- Runs on the post-slash-expansion text (the caller applies it after
  sanitization, before persistence, so the stored row matches the wire).

Table-driven on purpose: new keywords are data, not branches.
"""

from __future__ import annotations

import re
from typing import Any

# keyword -> hidden instruction appended to the turn's user message.
MAGIC_KEYWORDS: dict[str, str] = {
    "ultrathink": (
        "Ultra-think: reason very carefully and thoroughly before answering. "
        "Lay out the key considerations, check your work, and only then act."
    ),
}

_NOTICE_TAG = "[magic-keyword:{kw}]"

_FENCE_RE = re.compile(r"(^|\n)(```|~~~).*?(?:\n\2|$)", re.DOTALL)


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]


def _inside_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def matched_magic_keywords(text: str) -> list[str]:
    """Keyword names present in ``text`` outside fenced code, in table order."""
    if not text:
        return []
    spans = _fenced_spans(text)
    matched: list[str] = []
    for kw in MAGIC_KEYWORDS:
        pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
        for m in pattern.finditer(text):
            if not _inside_spans(m.start(), spans):
                matched.append(kw)
                break
    return matched


def apply_magic_keywords(user_message: Any) -> Any:
    """Append one hidden notice line per matched, not-yet-noticed keyword.

    Pure text transform: no state, no config — the table is the feature.
    """
    if not isinstance(user_message, str) or not user_message:
        return user_message
    notices: list[str] = []
    for kw in matched_magic_keywords(user_message):
        tag = _NOTICE_TAG.format(kw=kw)
        if tag in user_message:  # idempotent on resend/retry
            continue
        notices.append(f"{tag} {MAGIC_KEYWORDS[kw]}")
    if not notices:
        return user_message
    return user_message + "\n\n" + "\n".join(notices)
