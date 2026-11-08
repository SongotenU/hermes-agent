"""Outcome-aware synthetic results for tool calls whose results were never recorded.

When the outer turn loop dies (an escaped exception in response processing or tool
dispatch) while an assistant tool-call message is the tail, every unanswered call gets
a synthetic tool result so pairing holds. Those results must carry the call's recorded
lifecycle fact — not a bare "Error executing tool" line:

* a side-effecting call may have executed — its outcome is unknown; verify external
  state before retrying, retry only read-only/idempotent operations, never retry
  blindly (``effect_disposition`` "unknown");
* a read-only call changed nothing — retry is free (``effect_disposition`` "none").

Ported from DeepSeek Harness' ``ToolCallRecovery`` wording (``session/repair.ts``);
mirrors the per-call discrimination ``agent.replay_cleanup`` already applies on the
resume path, so every unrecorded-outcome surface speaks one language.
"""

from unittest.mock import patch

import agent.conversation_loop as loop
from agent.turn_loop_errors import handle_outer_loop_error


class _Agent:
    log_prefix = ""
    platform = "cli"
    suppress_status_output = True
    max_iterations = 50


def _assistant(*calls):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for cid, name in calls
        ],
    }


def _run_handler(messages, *, api_call_count=3, outer_errors=0, agent=None):
    with patch.object(loop, "_LOCAL_PROCESSING_MODULES", set()):
        try:
            raise RuntimeError("engine exploded")
        except RuntimeError as exc:
            return handle_outer_loop_error(
                agent or _Agent(), e=exc, _outer_error_count=outer_errors,
                api_call_count=api_call_count, messages=messages, conversation_history=[],
                _turn_exit_reason=None, failed=False, final_response="",
            )


def _tool_rows(messages):
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]


class TestUnansweredToolCallResults:
    def test_side_effecting_call_gets_unknown_outcome_guidance(self):
        messages = [{"role": "user", "content": "go"}, _assistant(("c1", "terminal"))]
        verdict = _run_handler(messages)

        # The loop may retry after a fallthrough; the results are filled either way.
        assert verdict.action == "fallthrough"
        (row,) = _tool_rows(messages)
        assert row["tool_call_id"] == "c1"
        assert row["name"] == "terminal"
        assert row["effect_disposition"] == "unknown"
        content = row["content"]
        assert "outcome is unknown" in content
        assert "Verify external state" in content
        assert "Do not retry blindly" in content
        assert "engine exploded" in content  # the recorded cause stays visible

    def test_read_only_call_reports_no_effect_and_is_retry_safe(self):
        messages = [{"role": "user", "content": "go"}, _assistant(("c1", "read_file"))]
        _run_handler(messages)

        (row,) = _tool_rows(messages)
        assert row["effect_disposition"] == "none"
        content = row["content"].lower()
        assert "read-only" in content
        assert "no lasting change" in content
        assert "unknown" not in content  # read-only: no unknown-outcome hedging

    def test_mixed_batch_discriminates_per_call(self):
        messages = [
            {"role": "user", "content": "go"},
            _assistant(("r", "read_file"), ("w", "write_file")),
        ]
        _run_handler(messages)

        rows = _tool_rows(messages)
        assert [r["tool_call_id"] for r in rows] == ["r", "w"]
        assert rows[0]["effect_disposition"] == "none"
        assert rows[1]["effect_disposition"] == "unknown"

    def test_answered_results_are_preserved_and_not_refilled(self):
        messages = [
            {"role": "user", "content": "go"},
            _assistant(("c1", "read_file"), ("c2", "terminal")),
            {"role": "tool", "name": "read_file", "tool_call_id": "c1", "content": "file body"},
        ]
        _run_handler(messages)

        rows = _tool_rows(messages)
        assert len(rows) == 2
        assert rows[0]["content"] == "file body"  # untouched
        assert rows[1]["tool_call_id"] == "c2"
        assert rows[1]["effect_disposition"] == "unknown"

    def test_terminal_exit_still_fills_results(self):
        agent = _Agent()
        agent.max_iterations = 3  # api_call_count 2 >= 3-1 -> near-limit break path
        messages = [{"role": "user", "content": "go"}, _assistant(("c1", "terminal"))]
        verdict = _run_handler(messages, api_call_count=2, agent=agent)

        assert verdict.action == "break"
        (row,) = _tool_rows(messages)
        assert row["effect_disposition"] == "unknown"
