"""In-history system updates (DSH A1 parity — the cache-safe third path).

Hermes defers every system-prompt-state change to the next session (prompt cache is
sacred); ``--now`` pays a full cache rebuild. DSH widens this: on routes whose provider
honours a ``system`` node appended AFTER the cached history
(``systemPromptUpdate: 'in-history'``), the change ships as a fresh system message at
the tail — the cached prefix stays byte-identical, so an update costs one new row, not
a rebuild.

Opt-in per route: config ``agent.in_history_system_updates: true`` AND ``api_mode``
``chat_completions`` (the shape the opencode-go spike proved). Every other route keeps
the deferral. Rows are append-only, one per update, never merged, never touching the
prefix (root prompt-caching invariant).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_PENDING_UPDATES = 8
MAX_UPDATE_CHARS = 4_000
ROW_PREFIX = "System update (applied mid-session, effective immediately):\n"

_PENDING_ATTR = "_pending_in_history_updates"


def _in_history_enabled() -> bool:
    """Config gate (``agent.in_history_system_updates``), defaults off."""
    try:
        from hermes_cli.config import load_config_readonly

        agent_cfg = (load_config_readonly() or {}).get("agent") or {}
        return bool(agent_cfg.get("in_history_system_updates", False))
    except Exception:
        logger.debug("in-history update config unreadable; staying deferred", exc_info=True)
        return False


def route_supports_in_history_updates(agent: Any) -> bool:
    """True when this agent's route honours a tail-appended system row."""
    api_mode = str(getattr(agent, "api_mode", "") or "")
    return api_mode in ("", "chat_completions") and _in_history_enabled()


def system_update_row(text: str) -> dict[str, Any]:
    """One standalone system row for a mid-session update."""
    body = (text or "").strip()[:MAX_UPDATE_CHARS]
    return {"role": "system", "content": ROW_PREFIX + body}


def queue_in_history_update(agent: Any, text: str) -> bool:
    """Queue an update for the next request build. False = route not capable (caller
    keeps its deferral). Bounded: the OLDEST pending update is dropped past the cap —
    a later update supersedes an earlier one describing the same evolving state."""
    if not text or not text.strip() or not route_supports_in_history_updates(agent):
        return False
    pending = getattr(agent, _PENDING_ATTR, None)
    if not isinstance(pending, list):
        pending = []
        setattr(agent, _PENDING_ATTR, pending)
    if len(pending) >= MAX_PENDING_UPDATES:
        dropped = pending.pop(0)
        logger.warning(
            "in-history updates over cap (%d): dropping oldest %r", MAX_PENDING_UPDATES, dropped[:80],
        )
    pending.append(text)
    return True


def drain_pending_in_history_updates(agent: Any) -> list[str]:
    pending = getattr(agent, _PENDING_ATTR, None)
    if not isinstance(pending, list) or not pending:
        return []
    setattr(agent, _PENDING_ATTR, [])
    return list(pending)


def inject_pending_in_history_updates(agent: Any, messages: list[dict[str, Any]]) -> int:
    """Append one system row per pending update at the messages tail. Returns the count.

    Capability is re-checked here because the route can change mid-turn (wire switch,
    fallback activation); when the current route cannot honour the rows, the queue is
    LEFT INTACT for the next capable route or the deferral notice."""
    if not route_supports_in_history_updates(agent):
        return 0
    texts = drain_pending_in_history_updates(agent)
    for text in texts:
        messages.append(system_update_row(text))
    if texts:
        logger.debug("in-history system updates appended: %d", len(texts))
    return len(texts)
