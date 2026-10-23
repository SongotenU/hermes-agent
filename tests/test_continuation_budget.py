"""Unit tests for agent.continuation_budget.ContinuationBudgetTracker."""

import pytest

from agent.continuation_budget import ContinuationBudgetTracker


class TestContinuationBudgetTracker:
    def test_below_min_continuations_never_stops(self):
        t = ContinuationBudgetTracker(min_continuations=3, diminishing_threshold=500)
        # Small deltas, but fewer than min_continuations recorded → keep going.
        assert t.record_continuation(100) is False   # count 1
        assert t.record_continuation(200) is False   # count 2
        assert t.record_continuation(300) is False   # count 3
        # 4th call: count(3) >= min(3), but only ONE small delta so far
        # (last_delta=100 from previous call, current delta=100) → stops.
        # Verify the boundary: with only 2 recorded small deltas it must NOT stop.
        t2 = ContinuationBudgetTracker(min_continuations=3, diminishing_threshold=500)
        assert t2.record_continuation(100) is False
        assert t2.record_continuation(900) is False  # big delta (800)
        assert t2.record_continuation(1000) is False  # count=3, small delta but prev big
        # count now 3, but last_delta=100 and current delta would need checking:
        # next small delta → last two small → stop
        assert t2.record_continuation(1100) is True

    def test_stop_fires_after_two_consecutive_small_deltas(self):
        t = ContinuationBudgetTracker(min_continuations=3, diminishing_threshold=500)
        assert t.record_continuation(100) is False  # count→1, delta=100
        assert t.record_continuation(200) is False  # count→2, delta=100
        assert t.record_continuation(300) is False  # count→3, delta=100
        # count(3) >= 3, delta=100 < 500, last_delta=100 < 500 → stop
        assert t.record_continuation(400) is True

    def test_large_delta_resets_diminishing_check(self):
        t = ContinuationBudgetTracker(min_continuations=3, diminishing_threshold=500)
        assert t.record_continuation(100) is False   # count 1
        assert t.record_continuation(200) is False   # count 2
        assert t.record_continuation(2000) is False  # count 3, delta=1800 (big)
        # delta=100 small, but last_delta=1800 not small → keep going
        assert t.record_continuation(2100) is False
        # now two consecutive small → stop
        assert t.record_continuation(2200) is True

    def test_delta_computed_from_global_tokens(self):
        t = ContinuationBudgetTracker(min_continuations=10, diminishing_threshold=500)
        t.record_continuation(1000)
        assert t.last_delta_tokens == 1000
        assert t.last_global_turn_tokens == 1000
        t.record_continuation(1500)
        assert t.last_delta_tokens == 500
        assert t.last_global_turn_tokens == 1500

    def test_zero_delta_counts_as_diminishing(self):
        t = ContinuationBudgetTracker(min_continuations=2, diminishing_threshold=500)
        assert t.record_continuation(500) is False  # count 1, delta 500 (not < 500)
        assert t.record_continuation(500) is False  # count 2, delta 0
        # count(2) >= 2, delta=0 < 500, last_delta=0 < 500 → stop
        assert t.record_continuation(500) is True

    def test_custom_thresholds(self):
        t = ContinuationBudgetTracker(min_continuations=1, diminishing_threshold=100)
        assert t.record_continuation(50) is False  # count 1
        # count(1) >= 1, delta=50 < 100, last_delta=50 < 100 → stop immediately
        assert t.record_continuation(100) is True

    def test_thread_safety_smoke(self):
        import concurrent.futures

        t = ContinuationBudgetTracker(min_continuations=1000, diminishing_threshold=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(t.record_continuation, range(1, 200)))
        assert all(r is False for r in results)
        assert t.continuation_count == 199
