"""Sliding-window error-rate breaker (ported design from xai-org/xai-circuit-breaker).

Grok trips on ``sample_count >= min_samples AND error_rate >= threshold`` over a
live time window — the case a consecutive-failure streak ladder misses (failures
dense but scattered, e.g. every other request). Tri-state Closed/Open/HalfOpen
with one half-open probe; a probe abandoned without a ``record()`` is reclaimed
after ``open_duration`` so a cancelled probe can never strand the breaker.

Adapted for Hermes: per-provider instances via ``breaker_for``; behaviour knobs
live in ``config.yaml`` (``auxiliary.breaker``), never env vars (secrets-only
policy); ``retry_after`` is the exact remaining open time.
"""

import pytest

from agent.sliding_window_breaker import (
    BreakerConfig,
    BreakerState,
    SlidingWindowBreaker,
    AuxiliaryBreakerOpen,
    breaker_for,
)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _breaker(**overrides):
    clock = FakeClock()
    cfg = BreakerConfig(
        window_seconds=overrides.pop("window_seconds", 60.0),
        min_samples=overrides.pop("min_samples", 4),
        failure_threshold=overrides.pop("failure_threshold", 0.5),
        open_seconds=overrides.pop("open_seconds", 10.0),
        half_open_max_probes=overrides.pop("half_open_max_probes", 1),
    )
    return SlidingWindowBreaker(cfg, clock=clock), clock


class TestTripCondition:
    def test_trips_on_dense_intermittent_failures(self):
        """Alternating fail/success never builds a streak — the window must still trip."""
        breaker, clock = _breaker()
        for i in range(6):
            breaker.record_failure()
            breaker.record_success()
            clock.advance(0.1)
        assert breaker.state() is BreakerState.OPEN

    def test_below_min_samples_never_trips(self):
        breaker, _ = _breaker(min_samples=10)
        for _ in range(9):
            breaker.record_failure()
        assert breaker.state() is BreakerState.CLOSED

    def test_low_rate_does_not_trip(self):
        breaker, clock = _breaker()
        for i in range(20):
            breaker.record_failure()
            for _ in range(4):
                breaker.record_success()
            clock.advance(0.01)
        # 20% error rate < 50% threshold.
        assert breaker.state() is BreakerState.CLOSED

    def test_samples_age_out_of_the_window(self):
        breaker, clock = _breaker(min_samples=4, window_seconds=10.0)
        for _ in range(8):
            breaker.record_failure()
        clock.advance(11.0)  # entire failure burst ages out
        for _ in range(2):
            breaker.record_success()
        assert breaker.state() is BreakerState.CLOSED


class TestOpenAndRetryAfter:
    def test_open_exposes_exact_retry_after(self):
        breaker, clock = _breaker(open_seconds=10.0)
        for _ in range(4):
            breaker.record_failure()
        assert breaker.state() is BreakerState.OPEN
        clock.advance(3.25)
        decision = breaker.allow()
        assert decision.allowed is False
        assert decision.retry_after == pytest.approx(6.75)

    def test_transitions_to_half_open_after_open_duration(self):
        breaker, clock = _breaker(open_seconds=10.0)
        for _ in range(4):
            breaker.record_failure()
        clock.advance(10.0)
        assert breaker.state() is BreakerState.HALF_OPEN
        decision = breaker.allow()
        assert decision.allowed is True and decision.is_probe is True


class TestHalfOpenProbe:
    def test_single_probe_then_reject_while_in_flight(self):
        breaker, clock = _breaker(open_seconds=10.0)
        for _ in range(4):
            breaker.record_failure()
        clock.advance(10.0)
        first = breaker.allow()
        second = breaker.allow()
        assert first.allowed and first.is_probe
        assert second.allowed is False
        assert second.retry_after is not None and second.retry_after > 0

    def test_probe_success_closes_and_resets_window(self):
        breaker, clock = _breaker(open_seconds=10.0)
        for _ in range(4):
            breaker.record_failure()
        clock.advance(10.0)
        assert breaker.allow().is_probe
        breaker.record_success()
        assert breaker.state() is BreakerState.CLOSED
        # The stale failure burst must not re-trip instantly.
        assert breaker.allow().allowed is True

    def test_probe_failure_reopens_for_full_duration(self):
        breaker, clock = _breaker(open_seconds=10.0)
        for _ in range(4):
            breaker.record_failure()
        clock.advance(10.0)
        assert breaker.allow().is_probe
        breaker.record_failure()
        assert breaker.state() is BreakerState.OPEN
        clock.advance(9.0)
        assert breaker.allow().allowed is False

    def test_abandoned_probe_lease_is_reclaimed(self):
        """A probe claimed but never recorded (cancelled request) must not strand HalfOpen."""
        breaker, clock = _breaker(open_seconds=10.0)
        for _ in range(4):
            breaker.record_failure()
        clock.advance(10.0)
        assert breaker.allow().is_probe  # probe claimed, then abandoned
        clock.advance(10.0)  # older than open_duration → lease reclaimed
        reclaimed = breaker.allow()
        assert reclaimed.allowed is True and reclaimed.is_probe is True


class TestAuxRegistry:
    def test_breaker_for_returns_same_instance_per_key(self):
        from agent.sliding_window_breaker import breaker_for, reset_breakers

        reset_breakers()
        try:
            assert breaker_for("prov-a") is breaker_for("prov-a")
            assert breaker_for("prov-a") is not breaker_for("prov-b")
        finally:
            reset_breakers()

    def test_non_countable_reason_never_counts(self):
        from agent.sliding_window_breaker import counts_toward_breaker

        assert counts_toward_breaker("server_error") is True
        assert counts_toward_breaker("timeout") is True
        assert counts_toward_breaker("overloaded") is True
        assert counts_toward_breaker("rate_limit") is True
        assert counts_toward_breaker("auth") is False
        assert counts_toward_breaker("format_error") is False
        assert counts_toward_breaker("unknown") is False

    def test_open_decision_raises_structured_error(self):
        from agent.sliding_window_breaker import guard_aux_attempt, reset_breakers

        reset_breakers()
        try:
            key = "prov-guard"
            b = breaker_for(key)
            b._cfg = BreakerConfig(min_samples=2, failure_threshold=0.5, open_seconds=10.0)
            b.record_failure()
            b.record_failure()
            with pytest.raises(AuxiliaryBreakerOpen) as info:
                guard_aux_attempt(key)
            assert info.value.retry_after > 0
            assert info.value.provider == key
        finally:
            reset_breakers()
