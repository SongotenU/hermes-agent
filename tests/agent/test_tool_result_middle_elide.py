"""Deterministic middle-elide for tool results without a bespoke summarizer (DSH parity).

Hermes already summarizes KNOWN tools into rich 1-line lines and prunes before any
LLM call. The gap was the generic fallback: an unknown tool's 200 KB result was
reduced to ``[name] (N chars result)`` — no content survives, so the model loses
both the beginning (what the output was) and the end (how it finished). DSH's
middle-prune keeps ``head + marker + tail`` bounded by a threshold; port that.
"""

from agent.context_compressor import _summarize_tool_result, _summarize_tool_result_unguarded


def _generic_body(lines: int = 2_000, width: int = 100) -> str:
    rows = [f"row {i:05d} " + "x" * (width - 10) for i in range(lines)]
    return "header line: build log follows\n" + "\n".join(rows) + "\nFINAL STATUS: SUCCESS"


def _generic_args() -> str:
    return '{"cmd": "make all"}'


class TestMiddleElide:

    def test_generic_tool_keeps_head_and_tail_with_marker(self):
        content = _generic_body()
        summary = _summarize_tool_result_unguarded("weird_tool", _generic_args(), content)

        assert summary.startswith("[weird_tool]")
        assert "header line: build log follows" in summary          # head survived
        assert "FINAL STATUS: SUCCESS" in summary                   # tail survived
        assert "middle elided" in summary                           # explicit marker
        assert "chars; head+tail" in summary                        # size is still reported
        elided = len(content) - summary.count("x")
        assert elided > 0

    def test_elided_summary_is_bounded(self):
        content = _generic_body(lines=50_000)  # ~5 MB
        summary = _summarize_tool_result("odd_tool", _generic_args(), content)
        # head+marker+tail well under any compression budget, unlike the raw body.
        assert len(summary) < 4_000

    def test_small_generic_body_keeps_old_one_line(self):
        tiny = "short output"
        summary = _summarize_tool_result_unguarded("weird_tool", _generic_args(), tiny)
        assert summary == "[weird_tool] cmd=make all (12 chars result)"

    def test_known_tool_summarizer_untouched(self):
        content = '{"exit_code": 0, "stdout": "done"}'
        summary = _summarize_tool_result_unguarded("terminal", '{"command": "ls"}', content)
        assert summary.startswith("[terminal] ran `ls` -> exit 0")

    def test_elided_stub_is_recognized_as_a_stub(self):
        """A later prune pass must not re-elide its own output (counter churn/anti-thrash)."""
        from agent.context_compressor import _is_summary_stub

        content = _generic_body()
        summary = _summarize_tool_result_unguarded("weird_tool", _generic_args(), content)
        assert _is_summary_stub(summary) is True

    def test_failure_suffix_survives_elision(self):
        import json

        content = json.dumps({"log": ["x" * 100] * 3_000, "error": "boom", "success": False})
        summary = _summarize_tool_result_unguarded("weird_tool", _generic_args(), content)
        assert "middle elided" in summary
        assert "FAILED: boom" in summary
