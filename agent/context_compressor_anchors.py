"""Mechanically harvested exact-identifier anchor ledger for context compression.

Split from ``agent/context_compressor.py`` (file-size ratchet; moved code keeps its cap).
The anchor index is LLM-free: regex-harvested exact identifiers from the compacted
region with per-category caps, most-frequent first, so needle facts (SHAs, ids, error
strings) cannot be paraphrased away; it doubles as a session_search map.

This module must never import ``agent.context_compressor`` at module level (import
cycle) — the one dependency (``_typed_user_text``) is late-imported inside the function.
"""

from __future__ import annotations

import re
from typing import Any

# Anchor ledger: mechanically harvested exact identifiers, no LLM, so needle facts
# (SHAs, ids, error strings) cannot be paraphrased away; also a session_search map.
_LEAN_ANCHOR_HEADING = "## Anchor Index (mechanically extracted, exact)"
_LEAN_ANCHOR_BUDGET_CHARS = 7_000
_ANCHOR_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("PRs/issues", re.compile(r"#\d{3,6}\b"), 120),
    ("commits", re.compile(r"\b[0-9a-f]{9,40}\b"), 40),
    ("branches", re.compile(r"\b(?:fix|feat|docs|refactor|chore|salvage|ent)/[A-Za-z0-9._/-]{3,60}"), 40),
    ("files", re.compile(r"\b[\w./-]+/[\w.-]+\.(?:py|ts|tsx|js|rs|md|yaml|yml|json|toml|sh)\b"), 80),
    ("errors", re.compile(r"\b(?:[A-Z][a-zA-Z]*Error|Exception|ENOSPC|EACCES|SIGKILL|Traceback)\b[^\n]{0,90}"), 40),
    ("handles", re.compile(r"@[A-Za-z0-9-]{3,30}\b"), 40),
    ("urls", re.compile(r"https?://[^\s)\"']{10,110}"), 30),
]
_ANCHOR_NOISE = frozenset({
    "@teknium", "@teknium1",  # session owner, in every transcript
})


def _build_anchor_index(turns: list[dict[str, Any]]) -> str:
    """Regex-harvest exact identifiers from the compacted region (LLM-free); per-category caps, most-frequent first."""
    from agent.context_compressor import _typed_user_text
    contents = (_typed_user_text(m["content"]) if m.get("role") == "user" and isinstance(m.get("content"), str) else m.get("content") for m in turns)
    text = "\n".join(c for c in contents if isinstance(c, str) and c)
    if not text:
        return ""
    sections: list[str] = []
    used = 0
    for label, pattern, cap in _ANCHOR_PATTERNS:
        counts: dict[str, int] = {}
        last_seen: dict[str, int] = {}
        for n, m in enumerate(pattern.finditer(text)):
            val = m.group(0).strip().rstrip(".,;:")
            if val.lower() in _ANCHOR_NOISE:
                continue
            counts[val] = counts.get(val, 0) + 1
            last_seen[val] = n
        if not counts:
            continue
        ranked = sorted(counts, key=lambda v: (-counts[v], -last_seen[v]))[:cap]
        line = f"{label}: " + ", ".join(f"{v}(x{counts[v]})" if counts[v] > 1 else v for v in ranked)
        if used + len(line) > _LEAN_ANCHOR_BUDGET_CHARS:
            break
        sections.append(line)
        used += len(line)
    if not sections:
        return ""
    return (
        "\n\n" + _LEAN_ANCHOR_HEADING + "\n"
        + "\n".join(sections)
        + "\n(Exact identifiers from the compacted region — use these verbatim, "
        "and as session_search query anchors to recover their full context.)"
    )
