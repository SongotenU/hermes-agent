"""Aux relay ↔ sliding-window breaker wiring.

Pins the seam contract added to ``agent.auxiliary_client``:
* an open breaker raises :class:`AuxiliaryBreakerOpen` BEFORE dispatch;
* only provider-side failure classes feed the window (format/request errors don't);
* a clean attempt records success; a probe success closes the breaker.
Exercised through the real ``aux_breaker_scope`` + registry, no client mocks.
"""

import pytest

from agent.sliding_window_breaker import (
    AuxiliaryBreakerOpen,
    BreakerConfig,
    BreakerState,
    aux_breaker_scope,
    breaker_for,
    reset_breakers,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_breakers()
    yield
    reset_breakers()


def _shrink(provider: str) -> None:
    breaker_for(provider)._cfg = BreakerConfig(
        window_seconds=60.0, min_samples=3, failure_threshold=0.5, open_seconds=10.0
    )


class _ServerError(RuntimeError):
    status_code = 500


class _FormatError(RuntimeError):
    status_code = 400


class TestScopeWiring:

    def test_server_errors_trip_the_breaker_and_block_dispatch(self):
        provider = "prov-500"
        _shrink(provider)
        for _ in range(3):
            with pytest.raises(_ServerError):
                with aux_breaker_scope(provider):
                    raise _ServerError("boom")
        assert breaker_for(provider).state() is BreakerState.OPEN
        dispatched = False
        with pytest.raises(AuxiliaryBreakerOpen) as info:
            with aux_breaker_scope(provider):
                dispatched = True
        assert dispatched is False  # raised BEFORE the body ran
        assert info.value.retry_after > 0

    def test_request_shape_errors_do_not_feed_the_window(self):
        provider = "prov-400"
        _shrink(provider)
        for _ in range(10):
            with pytest.raises(_FormatError):
                with aux_breaker_scope(provider):
                    raise _FormatError("bad shape")
        assert breaker_for(provider).state() is BreakerState.CLOSED

    def test_success_keeps_breaker_closed_and_counts_samples(self):
        provider = "prov-ok"
        _shrink(provider)
        for _ in range(5):
            with aux_breaker_scope(provider):
                pass
        assert breaker_for(provider).state() is BreakerState.CLOSED

    def test_half_open_probe_success_closes_after_open_expires(self):
        provider = "prov-probe"
        _shrink(provider)
        for _ in range(3):
            with pytest.raises(_ServerError):
                with aux_breaker_scope(provider):
                    raise _ServerError("boom")
        # Rewind the open clock instead of sleeping: settle next allow().
        breaker = breaker_for(provider)
        breaker._opened_at -= breaker._cfg.open_seconds + 0.1
        with aux_breaker_scope(provider):  # probe succeeds
            pass
        assert breaker.state() is BreakerState.CLOSED

    def test_keyless_attempts_pass_through(self):
        with aux_breaker_scope(""):
            pass
        with aux_breaker_scope(None):
            pass
