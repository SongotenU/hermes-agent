"""Sliding-window error-rate breaker for auxiliary provider calls.

Ported design (not code) from xai-org/xai-circuit-breaker: trip on
``sample_count >= min_samples AND error_rate >= threshold`` over a live time
window — the dense-intermittent failure pattern a consecutive-streak ladder
misses. Tri-state Closed/Open/HalfOpen, one half-open probe, and an
abandoned-probe lease (a probe claimed but never recorded is reclaimed after
``open_duration`` so a cancelled request can never strand the breaker in
HalfOpen). ``retry_after`` is the exact remaining open time.

Hermes adaptations:
* one breaker per provider key (``breaker_for``) — provider rotation/fallback
  already exists; this only gates *attempts to a sick provider*;
* knobs in config.yaml ``auxiliary.breaker`` (window_seconds, min_samples,
  failure_threshold, open_seconds) — never env vars (secrets-only policy);
* only provider-side failure classes count (``counts_toward_breaker``); auth,
  request-shape, policy and unknown reasons fail OPEN (no false trips).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerConfig:
    window_seconds: float = 60.0
    min_samples: int = 5
    failure_threshold: float = 0.5
    open_seconds: float = 10.0
    half_open_max_probes: int = 1


@dataclass
class AllowDecision:
    allowed: bool
    retry_after: Optional[float] = None
    is_probe: bool = False


# Failure classes that indicate provider unreliability (transient server-side).
_COUNTED_REASONS = frozenset({
    "rate_limit", "upstream_rate_limit", "upstream_blocked", "overloaded",
    "server_error", "timeout", "incomplete_response",
})


def counts_toward_breaker(reason: str) -> bool:
    """True when an aux failure with this classifier reason should count."""
    return reason in _COUNTED_REASONS


class SlidingWindowBreaker:
    """Windowed failure-rate breaker with half-open probe leases."""

    def __init__(self, config: Optional[BreakerConfig] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._cfg = config or BreakerConfig()
        self._clock = clock
        self._samples: deque[tuple[float, bool]] = deque()  # (timestamp, failed)
        self._state = BreakerState.CLOSED
        self._opened_at: Optional[float] = None
        self._probe_leases: list[float] = []
        self._lock = threading.Lock()

    # -- state ---------------------------------------------------------------
    def _settle(self, now: float) -> None:
        if self._state is BreakerState.OPEN and self._opened_at is not None:
            if now - self._opened_at >= self._cfg.open_seconds:
                self._state = BreakerState.HALF_OPEN
        if self._state is BreakerState.HALF_OPEN:
            horizon = now - self._cfg.open_seconds
            self._probe_leases = [lease for lease in self._probe_leases if lease > horizon]

    def _expire_samples(self, now: float) -> None:
        horizon = now - self._cfg.window_seconds
        while self._samples and self._samples[0][0] <= horizon:
            self._samples.popleft()

    def state(self) -> BreakerState:
        with self._lock:
            self._settle(self._clock())
            return self._state

    # -- decision ------------------------------------------------------------
    def allow(self) -> AllowDecision:
        with self._lock:
            now = self._clock()
            self._settle(now)
            if self._state is BreakerState.CLOSED:
                return AllowDecision(allowed=True)
            if self._state is BreakerState.OPEN:
                remaining = self._cfg.open_seconds - (now - (self._opened_at or now))
                return AllowDecision(allowed=False, retry_after=max(remaining, 0.0))
            # HALF_OPEN: grant at most half_open_max_probes live probes.
            if len(self._probe_leases) < self._cfg.half_open_max_probes:
                self._probe_leases.append(now)
                return AllowDecision(allowed=True, is_probe=True)
            soonest = min(lease + self._cfg.open_seconds for lease in self._probe_leases)
            return AllowDecision(allowed=False, retry_after=max(soonest - now, 0.05))

    # -- outcomes --------------------------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            now = self._clock()
            self._settle(now)
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.CLOSED
                self._opened_at = None
                self._probe_leases.clear()
                self._samples.clear()
                return
            self._samples.append((now, False))
            self._expire_samples(now)

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._settle(now)
            if self._state is BreakerState.HALF_OPEN:
                self._open(now)  # probe failed: full new open period
                return
            self._samples.append((now, True))
            self._expire_samples(now)
            if len(self._samples) >= self._cfg.min_samples:
                failures = sum(1 for _, failed in self._samples if failed)
                if failures / len(self._samples) >= self._cfg.failure_threshold:
                    self._open(now)

    def _open(self, now: float) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = now
        self._probe_leases.clear()
        self._samples.clear()  # post-open probe success must not insta-retrip


class AuxiliaryBreakerOpen(RuntimeError):
    """The provider's breaker is open: skip the attempt, honor ``retry_after``."""

    def __init__(self, provider: str, retry_after: float):
        super().__init__(
            f"auxiliary breaker open for provider {provider!r}; retry after {retry_after:.2f}s"
        )
        self.provider = provider
        self.retry_after = retry_after


# -- per-provider registry -----------------------------------------------------

_breakers: dict[str, SlidingWindowBreaker] = {}
_registry_lock = threading.Lock()


def _config_from_yaml() -> BreakerConfig:
    """``auxiliary.breaker`` knobs; malformed values fall back to defaults."""
    defaults = BreakerConfig()
    try:
        from hermes_cli.config import load_config_readonly

        raw = ((load_config_readonly() or {}).get("auxiliary") or {}).get("breaker") or {}
        if not isinstance(raw, dict):
            return defaults
        return BreakerConfig(
            window_seconds=float(raw.get("window_seconds", defaults.window_seconds)),
            min_samples=int(raw.get("min_samples", defaults.min_samples)),
            failure_threshold=float(raw.get("failure_threshold", defaults.failure_threshold)),
            open_seconds=float(raw.get("open_seconds", defaults.open_seconds)),
        )
    except Exception:
        # Telemetry-adjacent config: defaults are always a safe answer.
        logger.debug("aux breaker config unreadable; using defaults", exc_info=True)
        return defaults


def breaker_for(key: str) -> SlidingWindowBreaker:
    with _registry_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            breaker = SlidingWindowBreaker(_config_from_yaml())
            _breakers[key] = breaker
        return breaker


def reset_breakers() -> None:
    with _registry_lock:
        _breakers.clear()


def guard_aux_attempt(provider: str) -> None:
    """Raise :class:`AuxiliaryBreakerOpen` when the provider's breaker is open."""
    if not provider:
        return
    decision = breaker_for(provider).allow()
    if not decision.allowed:
        raise AuxiliaryBreakerOpen(provider, decision.retry_after or 0.0)


def note_aux_outcome(provider: str, *, failure_reason: Optional[str]) -> None:
    """Record one physical aux attempt outcome (``None`` reason = success)."""
    if not provider:
        return
    breaker = breaker_for(provider)
    if failure_reason is None:
        breaker.record_success()
    elif counts_toward_breaker(failure_reason):
        breaker.record_failure()


@contextlib.contextmanager
def aux_breaker_scope(provider: str):
    """Gate one physical aux relay attempt: open breaker → raise before dispatch;
    exception → classified failure (counted only for provider-side classes);
    clean return → success. Keyless calls pass through untouched."""
    provider = provider or ""
    if not provider:
        yield
        return
    guard_aux_attempt(provider)
    try:
        yield
    except AuxiliaryBreakerOpen:
        raise
    except Exception as exc:
        reason = "unknown"
        try:
            from agent import auxiliary_call_outcome

            reason = auxiliary_call_outcome.error_class(exc)
        except Exception:
            logger.debug("aux breaker failure classification failed", exc_info=True)
        note_aux_outcome(provider, failure_reason=reason)
        raise
    else:
        note_aux_outcome(provider, failure_reason=None)
