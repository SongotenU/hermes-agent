"""Transient progress for tool calls made INSIDE ``execute_code`` (sandbox RPC sub-calls).

DeepSeek Harness surfaces every code-driven tool dispatch as a visible event
(``tool/ptc-dispatch``); Hermes keeps the sandbox call log internal
(``tools/code_execution_rpc.tool_call_log``). This module is the seam that lets each
finished sub-call reach the surfaces as TRANSIENT progress chrome (never a durable
transcript row): the executor installs an emitter for the duration of an
``execute_code`` dispatch, and the RPC chokepoint calls :func:`emit_subcall` after
each dispatched call.

Reachability: the emitter rides a ContextVar. Per-call sandbox RPC threads are spawned
through ``tools.thread_context.propagate_context_to_thread`` (copies the context), and
a session kernel's ``CellAuthority`` captures ``contextvars.copy_context()`` from the
calling thread and re-installs it per dispatch — so both RPC paths see the emitter of
the execute_code call that owns them, with no staleness across calls (a retired cell
refuses dispatch before emitting).

Never raises: progress chrome must not break a sandbox call.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: ``(tool_name, args_preview, duration_s, error_or_None) -> None``
SubcallEmitter = Callable[[str, str, float, Optional[str]], None]

_subcall_emitter: contextvars.ContextVar[Optional[SubcallEmitter]] = contextvars.ContextVar(
    "subcall_progress_emitter", default=None
)


def set_subcall_emitter(emitter: Optional[SubcallEmitter]) -> contextvars.Token:
    """Install the emitter for the current context; reset with the returned token."""
    return _subcall_emitter.set(emitter)


def reset_subcall_emitter(token: contextvars.Token) -> None:
    _subcall_emitter.reset(token)


def current_subcall_emitter() -> Optional[SubcallEmitter]:
    return _subcall_emitter.get()


def make_agent_emitter(agent, parent_tool: str = "execute_code") -> SubcallEmitter:
    """Emitter reporting each finished sub-call to *agent*'s ``tool_progress_callback``.

    Best-effort by construction: :func:`emit_subcall` already contains a raising surface.
    """
    def _emit(tool_name: str, args_preview: str, duration_s: float, error: Optional[str]) -> None:
        callback = agent.tool_progress_callback
        if callback is None:
            return
        callback("tool.subcall", tool_name, args_preview, None,
                 parent=parent_tool, duration=duration_s, error=error)
    return _emit


def emit_subcall(tool_name: str, args_preview: Any, duration_s: Any, error: Any = None) -> None:
    """Best-effort: report one finished sandbox sub-call to the installed emitter."""
    emitter = _subcall_emitter.get()
    if emitter is None:
        return
    try:
        emitter(str(tool_name or ""), str(args_preview or ""), float(duration_s or 0.0),
                str(error) if error else None)
    except Exception:
        logger.debug("subcall progress emission failed", exc_info=True)
