"""Degenerate-summary gate (ported from xai-org/grok-build's is_degenerate_summary).

A cleaned summary below the size-scaled floor is a transient failure: one main-model retry,
then ABORT preserving the session — it is never committed as the compaction checkpoint.
Split from test_context_compressor.py per the file-size ratchet (new tests live in a new
test_<stem>_<topic>.py).
"""

from __future__ import annotations

from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.context_compressor_failures import _DEGENERATE_SUMMARY_MARKER, _classify_summary_failure


def _msgs(n=10):
    # Bulky bodies so the serialized transcript clears the degenerate gate's floor
    # (floor = input//20 capped 500): "Done." must register as degenerate.
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i} " + ("context detail " * 12)}
        for i in range(n)
    ]


def test_generate_summary_flags_degenerate_failure():
    """A cleaned summary shorter than the floor cannot have covered the compacted
    turns: it flags _last_summary_degenerate_failure and is never committed as
    _previous_summary."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(model="test", quiet_mode=True)
    with patch(
        "agent.context_compressor.call_llm",
        return_value={"choices": [{"message": {"content": "Done."}}]},
    ):
        result = c._generate_summary(_msgs())
    assert result is None
    assert c._last_summary_degenerate_failure is True
    assert c._last_summary_empty_content_failure is False
    assert c._last_summary_truncated_failure is False


def test_degenerate_summary_aborts_compression_and_preserves_messages():
    """A degenerate summary aborts compression and preserves original messages
    without dropping context (same ladder as a truncated summary)."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(
            model="test",
            quiet_mode=True,
            protect_first_n=2,
            protect_last_n=2,
            abort_on_summary_failure=False,
        )
    msgs = _msgs(12)
    with patch(
        "agent.context_compressor.call_llm",
        return_value={"choices": [{"message": {"content": "Done."}}]},
    ):
        result = c.compress(msgs, current_tokens=999999, force=True)
    assert result == msgs
    assert c._last_summary_degenerate_failure is True
    assert c._last_compress_aborted is True
    assert c._last_summary_dropped_count == 0
    # Cooldown re-entry must keep aborting: _generate_summary() returns None from
    # the cooldown early-return without re-asserting the flag, so compress() must
    # still see it (same shape as the empty-content abort test).
    second = c.compress(msgs, current_tokens=999999)
    assert second == msgs
    assert c._last_compress_aborted is True


def test_classify_summary_failure_degenerate_marker():
    """The degenerate marker classifies into its own kind with a specific
    fallback reason (so the main-model retry log names it)."""
    kind = _classify_summary_failure(
        RuntimeError(
            f"Context compression summary was degenerate ({_DEGENERATE_SUMMARY_MARKER}): "
            "5 chars < 500-char seed floor"
        )
    )
    assert kind.degenerate is True
    assert kind.fallback_reason() == "returned a degenerate summary (too short to cover the compacted turns)"


def test_summary_at_seed_floor_still_commits():
    """Boundary control: a cleaned summary at/above the floor commits normally and
    never trips the degenerate flag."""
    long_summary = "## Goal\n" + ("filled context line for the transcript summary\n" * 20)
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        c = ContextCompressor(model="test", quiet_mode=True)
    with patch(
        "agent.context_compressor.call_llm",
        return_value={"choices": [{"message": {"content": long_summary}}]},
    ):
        result = c._generate_summary(_msgs())
    assert result is not None
    assert c._last_summary_degenerate_failure is False
