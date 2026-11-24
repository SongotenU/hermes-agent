"""Request-epoch logging for the conversation loop (DeepSeek Harness port).

Every conversation request logs its envelope epoch once: the request envelope — model,
provider, api_mode, base_url, system prompt, tool schemas, prompt-cache scope — is
fingerprinted and compared with the previous request of this agent instance.

* ``reason=change`` names WHY the provider-side prompt cache can no longer hit
  (``changes=model,tools,…``; route values print old->new).
* ``reason=series`` is an unchanged envelope that follows a history replacement
  (session rotation after compression, or a shrunken transcript) — the cache prefix
  was intentionally reset.
* Ordinary append-only requests log nothing: silence means the envelope is warm.

Vocabulary mirrors DSH's ``request/header`` event: ``initial`` (first request of this
instance), ``resume`` (first request carrying restored history — the provider cache may
still be warm if the bytes match), ``change``, ``series`` (carrying
``starts_series=true``; a ``change`` that also restarts the series carries the flag
too). Log-only: never part of the model transcript and never raises — cache-debug
observability on the hot request path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger("agent.conversation_loop")

#: Envelope fields compared field-wise; a change in any of these starts a new epoch.
_ROUTE_FIELDS = ("model", "provider", "api_mode", "base_url")
_FINGERPRINT_FIELDS = ("system", "tools", "scope")
_COMPARED_FIELDS = (*_ROUTE_FIELDS, *_FINGERPRINT_FIELDS)


def _short_hash(payload: Any) -> str:
    """12-hex-char fingerprint of a prompt/tools payload (canonical JSON for non-strings)."""
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            payload = str(payload)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:12]


def _fingerprint(agent: Any, system_message: Any, tools_for_api: Any) -> dict:
    scope = ""
    try:
        from agent.chat_completion_helpers import _prompt_cache_scope_for_agent

        scope = _prompt_cache_scope_for_agent(agent) or ""
    except Exception:
        logger.debug("request-epoch scope resolution failed", exc_info=True)
    return {
        "model": str(getattr(agent, "model", "") or ""),
        "provider": str(getattr(agent, "provider", "") or ""),
        "api_mode": str(getattr(agent, "api_mode", "") or ""),
        "base_url": str(getattr(agent, "base_url", "") or ""),
        "system": _short_hash(system_message if isinstance(system_message, str) else str(system_message or "")),
        "tools": _short_hash(tools_for_api or []),
        "scope": scope,
    }


def log_request_epoch(
    agent: Any, *, system_message: Any, tools_for_api: Any, api_messages: Any,
) -> None:
    """Compare this request's envelope with the previous one and log the epoch line.

    State lives on the agent instance (``agent._request_epoch``); a fresh instance
    logs ``initial`` (no restored history) or ``resume`` (restored history). Never
    raises.
    """
    try:
        current = _fingerprint(agent, system_message, tools_for_api)
        prev = getattr(agent, "_request_epoch", None)
        messages_len = len(api_messages) if isinstance(api_messages, (list, tuple)) else 0
        session_id = str(getattr(agent, "session_id", "") or "")
        prefix = getattr(agent, "log_prefix", "") or ""

        if prev is None:
            reason = "resume" if messages_len > 2 else "initial"
            logger.info(
                "%sRequest epoch: reason=%s model=%s provider=%s api_mode=%s system=%s tools=%s scope=%s messages=%d",
                prefix, reason, current["model"], current["provider"], current["api_mode"],
                current["system"], current["tools"], current["scope"] or "-", messages_len,
            )
        else:
            changed = [k for k in _COMPARED_FIELDS if current[k] != prev.get(k)]
            starts_series = session_id != prev.get("session_id") or messages_len < prev.get("messages_len", 0)
            if changed:
                detail = ",".join(
                    f"{k}={prev.get(k, '?')}->{current[k]}" if k in _ROUTE_FIELDS else k
                    for k in changed
                )
                logger.info(
                    "%sRequest epoch: reason=change changes=%s (%s)%s",
                    prefix, ",".join(changed), detail,
                    " starts_series=true" if starts_series else "",
                )
            elif starts_series:
                logger.info("%sRequest epoch: reason=series starts_series=true", prefix)

        current["session_id"] = session_id
        current["messages_len"] = messages_len
        agent._request_epoch = current
    except Exception:
        logger.debug("request-epoch logging failed", exc_info=True)
