"""Logical-call lifecycle helpers for auxiliary relay attempts.

Split from ``agent/auxiliary_client.py`` (file-size ratchet); bodies moved
verbatim except for the late-bound imports of the shared ContextVar and
``relay_llm`` (facade pattern — read ``agent.auxiliary_client`` at call time so
patching the facade still intercepts).
"""

from __future__ import annotations

from typing import Optional


def _complete_relay_auxiliary_call(*, outcome: str = "success", error_class: Optional[str] = None) -> None:
    """Close one auxiliary logical call after acceptance or terminal failure. A success
    reports the last attempt error it recovered from (``none`` when the first attempt held)."""
    from agent.auxiliary_client import _RELAY_AUX_CALL_CONTEXT
    from agent import relay_llm

    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is None:
        return
    relay_llm.complete_logical_call(
        str(context.get("request_id") or ""), outcome=outcome,
        model_name=str(context.get("model") or "unknown"),
        provider_name=str(context.get("provider") or "auxiliary"),
        response_model_name=context.get("response_model"),
        error_class=error_class or context.get("error_class") or "none",
    )


def _note_relay_auxiliary_error(exc: BaseException) -> None:
    """Remember one failed attempt's classified reason on the logical call."""
    from agent.auxiliary_client import _RELAY_AUX_CALL_CONTEXT

    context = _RELAY_AUX_CALL_CONTEXT.get()
    if context is not None and isinstance(exc, Exception):
        from agent import auxiliary_call_outcome
        context["error_class"] = auxiliary_call_outcome.error_class(
            exc, provider=str(context.get("provider") or ""), model=str(context.get("model") or ""))


def _fail_relay_auxiliary_call(exc: BaseException) -> None:
    """Close a call that ended in ``exc`` without replacing it: a Hermes abort is ``cancelled``,
    anything else ``failed`` with the classifier's reason for the error that ended it."""
    try:
        from agent import auxiliary_call_outcome
        if auxiliary_call_outcome.is_cancellation(exc):
            _complete_relay_auxiliary_call(outcome="cancelled", error_class="none")
            return
        _note_relay_auxiliary_error(exc)
        _complete_relay_auxiliary_call(outcome="failed")
    except Exception:
        # Closing bookkeeping must never replace the caller's exception.
        import logging
        logging.getLogger(__name__).debug("aux relay call close failed", exc_info=True)
