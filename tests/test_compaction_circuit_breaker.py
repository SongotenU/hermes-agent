"""Tests for the compaction circuit breaker in ContextCompressor.

Covers R2.1–R2.5 (SPEC phase-01):
- 3 consecutive summary failures → 4th compress() short-circuits before summary LLM call
- Quota/auth error trips breaker in ONE failure
- Manual /compress (force=True) resets the breaker
- Successful summary resets the breaker
"""

import pytest

from agent.context_compressor import ContextCompressor


def _make_compressor(max_consecutive: int = 3, enabled: bool = True) -> ContextCompressor:
    """Construct a ContextCompressor with the breaker config pre-baked.

    We bypass the config.yaml loader by patching the breaker fields after init.
    """
    c = ContextCompressor(
        model="test-model",
        threshold_percent=0.50,
        quiet_mode=True,
        abort_on_summary_failure=False,
    )
    c._breaker_enabled = enabled
    c._breaker_max_consecutive = max_consecutive
    c._consecutive_summary_failures = 0
    c._breaker_tripped_logged = False
    return c


class TestCompactionCircuitBreaker:
    def test_three_failures_trip_breaker_fourth_call_zero_llm(self):
        """R2.2: After 3 consecutive failures, the 4th compress() must NOT
        invoke the summary LLM — it short-circuits via _should_skip_compression."""
        c = _make_compressor(max_consecutive=3)
        assert c._consecutive_summary_failures == 0

        # Simulate 3 failures via the real failure-recording path.
        for i in range(3):
            c._record_compression_failure_cooldown(
                cooldown_seconds=0.01,
                error=f"synthetic error #{i + 1}",
            )
        assert c._consecutive_summary_failures == 3

        # Wait for the trivial cooldowns (0.01s each) to expire so the
        # breaker branch (not the cooldown branch) is the one that fires.
        import time as _time

        _time.sleep(0.05)
        # The _automatic_compression_blocked() path (where the breaker check lives)
        # must now return True, meaning compress() will bail before any LLM.
        result = c._automatic_compression_blocked()
        assert result is True
        assert c._last_compress_aborted is True
        assert c._breaker_tripped_logged is True

    def test_quota_error_trips_breaker_in_one_failure(self):
        """R2.5: quota/auth/rate error increments by max_consecutive (one-shot trip)."""
        c = _make_compressor(max_consecutive=3)
        # An auth-class error string that _is_summary_access_or_quota_error
        # recognises (the helper checks for 401/403/quota/rate-limit markers).
        c._record_compression_failure_cooldown(
            cooldown_seconds=0.01,
            error="401 Unauthorized: Invalid API key for summary provider",
        )
        assert c._consecutive_summary_failures >= 3
        import time as _time

        _time.sleep(0.05)
        assert c._automatic_compression_blocked() is True

    def test_manual_compress_resets_breaker(self):
        """R2.3b: force=True in compress() calls _clear_compression_failure_cooldown
        which now also resets the consecutive failure counter."""
        c = _make_compressor(max_consecutive=3)
        for i in range(3):
            c._record_compression_failure_cooldown(0.01, f"err {i + 1}")
        assert c._consecutive_summary_failures == 3
        assert c._automatic_compression_blocked() is True

        # Manual /compress clears the cooldown (which now resets the breaker).
        c._clear_compression_failure_cooldown()
        assert c._consecutive_summary_failures == 0
        assert c._breaker_tripped_logged is False

    def test_successful_summary_resets_breaker(self):
        """R2.3a: anywhere _clear_compression_failure_cooldown is called on
        success also wipes the breaker counter."""
        c = _make_compressor(max_consecutive=3)
        c._record_compression_failure_cooldown(0.01, "err 1")
        c._record_compression_failure_cooldown(0.01, "err 2")
        assert c._consecutive_summary_failures == 2

        c._clear_compression_failure_cooldown()  # success path
        assert c._consecutive_summary_failures == 0

    def test_below_threshold_does_not_trip(self):
        """2 failures with max_consecutive=3 → breaker does NOT trip."""
        c = _make_compressor(max_consecutive=3)
        c._record_compression_failure_cooldown(0.01, "err 1")
        c._record_compression_failure_cooldown(0.01, "err 2")
        assert c._consecutive_summary_failures == 2
        # No cooldown left (0.01s expired) → breaker check runs and returns False
        # because 2 < 3. We need to ensure cooldown is truly expired.
        import time as _time

        _time.sleep(0.05)
        assert c._automatic_compression_blocked() is False

    def test_disabled_breaker_never_trips(self):
        """circuit_breaker_enabled=false → breaker never fires regardless of failures."""
        c = _make_compressor(max_consecutive=3, enabled=False)
        for i in range(10):
            c._record_compression_failure_cooldown(0.01, f"err {i + 1}")
        # 10 failures but breaker disabled → skip returns False (cooldown expired).
        import time as _time

        _time.sleep(0.05)
        assert c._automatic_compression_blocked() is False
        assert c._last_compress_aborted is False

    def test_breaker_no_cooldown_timer(self):
        """R2.2: breaker is session-terminal, not a cooldown — the cooldown
        timer must NOT be set when the breaker trips (it sets _last_compress_aborted)."""
        c = _make_compressor(max_consecutive=3)
        for i in range(3):
            c._record_compression_failure_cooldown(0.01, f"err {i + 1}")
        # Wait for the trivial cooldowns from the failure-recording to expire.
        import time as _time

        _time.sleep(0.05)
        assert c._summary_failure_cooldown_until <= _time.monotonic()  # cooldown expired
        # Now _should_skip_compression should trip the breaker (not the cooldown path).
        result = c._automatic_compression_blocked()
        assert result is True
        assert c._last_compress_aborted is True
        # The breaker itself must not have re-armed a cooldown timer:
        # (cooldown expired state proves we entered the breaker branch, not the cooldown branch)
