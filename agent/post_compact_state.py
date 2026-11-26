"""Post-compaction active-state reminder (Grok ``reminder.rs`` parity).

Compression rewrites history down to the summary; work that is still LIVE —
background processes and delegated subagents — otherwise vanishes from the model's
view, and the post-compact agent reports stale facts or re-spawns duplicates. The
todo snapshot already re-injects (`_fold_todo_snapshot`); this adds the other half:
running background tasks and live delegations, delivered as a synthetic user row
at the same boundary (strip-and-refresh, never mid-prefix).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

ACTIVE_STATE_MARKER = "[Active state reminder]"


def compose_active_state_block(
    running_procs: list[dict[str, Any]], live_delegations: bool,
) -> Optional[str]:
    """Pure composer: None when nothing is live (no empty reminders)."""
    lines: list[str] = []
    if running_procs:
        shown = running_procs[:5]
        lines.append(f"{len(running_procs)} background process(es) still RUNNING:")
        for proc in shown:
            ident = str(proc.get("process_id") or proc.get("id") or "?")
            cmd = str(proc.get("command") or "").strip()
            cmd = cmd if len(cmd) <= 80 else cmd[:77] + "..."
            lines.append(f"- {ident}: {cmd}")
        if len(running_procs) > len(shown):
            lines.append(f"- … and {len(running_procs) - len(shown)} more")
    if live_delegations:
        lines.append("Delegated subagent(s) are still LIVE; their results will arrive as completions — "
                     "do not re-spawn or assume failure.")
    if not lines:
        return None
    return ACTIVE_STATE_MARKER + "\n" + "\n".join(lines)


def collect_running_procs(agent: Any) -> list[dict[str, Any]]:
    """Running background processes owned by this agent's task; [] when unknowable."""
    task_id = getattr(agent, "task_id", None)
    if not task_id:
        return []
    try:
        from tools.process_registry import process_registry

        procs = process_registry.running_owned_by(str(task_id))
        return [
            {"process_id": getattr(p, "process_id", "?"), "command": getattr(p, "command", "")}
            for p in procs
        ]
    except Exception:
        logger.debug("active-state: process registry unavailable", exc_info=True)
        return []


def live_delegations(agent: Any) -> bool:
    try:
        from tools import async_delegation

        return bool(async_delegation.has_live_for_session(
            parent_session_id=str(getattr(agent, "session_id", "") or ""),
        ))
    except Exception:
        logger.debug("active-state: delegation registry unavailable", exc_info=True)
        return False


def build_active_state_block(agent: Any) -> Optional[str]:
    return compose_active_state_block(collect_running_procs(agent), live_delegations(agent))


def _strip_prior_rows(messages: list) -> None:
    """Remove a previously injected reminder row so repeated compactions refresh."""
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.startswith(ACTIVE_STATE_MARKER):
            messages.pop(idx)


def fold_active_state_reminder(agent: Any, messages: list) -> None:
    """Strip stale reminders, then append the live one as a synthetic user row.
    Fully self-guarded: a reminder must never break the compression it describes."""
    try:
        _strip_prior_rows(messages)
        block = build_active_state_block(agent)
        if not block:
            return
        messages.append({"role": "user", "content": block, "_active_state_synthetic": True})
    except Exception:
        logger.debug("active-state reminder skipped", exc_info=True)
