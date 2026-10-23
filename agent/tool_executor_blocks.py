"""Block-result synthesis for the tool executor: the results for calls blocked by
scope / plugin / pruned-args (``block_body``) or by guardrail policy, and the
``pre_tool_call`` plugin gate. Split out of ``agent/tool_executor.py`` (facade ratchet)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from agent.inline_tool_executors import tool_hook_ids

if TYPE_CHECKING:  # the executor owns the type; a runtime import would be a cycle
    from agent.tool_executor import _ToolCallRef

logger = logging.getLogger(__name__)


def _blocked_tool_result(agent, ref: _ToolCallRef, *, block_body: dict[str, Any] | None,
                         block_error_type: str, guardrail_decision) -> str:
    """Synthesize the result for a call blocked by scope/plugin/pruned-args (``block_body``, the
    JSON the model sees) or by guardrail policy (``guardrail_decision``) and emit its terminal post_tool_call."""
    if block_body is not None:
        result = json.dumps(block_body, ensure_ascii=False)
        error_type, error_message = block_error_type, block_body.get("message") or block_body["error"]
    else:
        result = agent._guardrail_block_result(guardrail_decision)
        error_type = "guardrail_block"
        error_message = getattr(guardrail_decision, "message", None) or "Tool blocked by guardrail policy"
    ref.emit_post(agent, result, status="blocked", error_type=error_type, error_message=error_message)
    return result


def _pre_tool_block(agent, ref: _ToolCallRef):
    """Run ``pre_tool_call`` plugin hooks; returns ``(block_message, final_args)`` with any
    hook-modified args applied. Hook failures never block."""
    try:
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

        block_msg, modified_args = _dispatch_pre_tool_call_hooks(
            ref.name,
            ref.args,
            **tool_hook_ids(agent, ref.task_id, ref.call_id),
            middleware_trace=list(ref.trace),
        )
        return block_msg, (ref.args if modified_args is None else modified_args)
    except Exception:
        logger.debug("pre_tool_call block hook dispatch failed", exc_info=True)
        return None, ref.args
