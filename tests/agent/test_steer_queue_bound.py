"""Pending-steer buffer is bounded (Grok ``xai-interjection-core`` parity:
``push_capped`` — capped queue, drop-oldest, per-entry truncation).

An unbounded steer buffer lets a spamming client grow agent memory without
limit between tool batches; the cap keeps the newest submissions and bounds
each entry's size."""

import threading
from types import SimpleNamespace

from agent.interrupt_control import (
    InterruptControlMixin,
    STEER_ENTRY_MAX_CHARS,
    STEER_QUEUE_MAX_ENTRIES,
    _steer_parts,
)


def _agent():
    return SimpleNamespace(
        _pending_steer=None,
        _pending_steer_lock=threading.Lock(),
    )


class TestSteerQueueBound:

    def test_queue_caps_at_max_entries_dropping_oldest(self):
        bound = _agent()
        for i in range(STEER_QUEUE_MAX_ENTRIES + 10):
            assert InterruptControlMixin.steer(bound, f"steer-{i}") is True
        parts = _steer_parts(bound)
        assert len(parts) == STEER_QUEUE_MAX_ENTRIES
        # Oldest ten dropped; newest survives; order preserved.
        assert parts[0][1] == "steer-10"
        assert parts[-1][1] == f"steer-{STEER_QUEUE_MAX_ENTRIES + 9}"

    def test_entry_truncates_to_max_chars(self):
        bound = _agent()
        InterruptControlMixin.steer(bound, "x" * (STEER_ENTRY_MAX_CHARS + 5_000))
        parts = _steer_parts(bound)
        assert len(parts) == 1
        text = parts[0][1]
        assert len(text) <= STEER_ENTRY_MAX_CHARS + len("\n… [steer truncated]")
        assert text.endswith("[steer truncated]")

    def test_small_steer_untouched(self):
        bound = _agent()
        InterruptControlMixin.steer(bound, "  hello  ")
        assert _steer_parts(bound) == [(None, "hello")]

    def test_ticket_preserved(self):
        bound = _agent()
        InterruptControlMixin.steer(bound, "old", ticket="t-old")
        InterruptControlMixin.steer(bound, "new", ticket="t-new")
        assert _steer_parts(bound) == [("t-old", "old"), ("t-new", "new")]
