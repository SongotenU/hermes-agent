"""Tests for the opt-in idle-triggered compaction policy.

Covers ``agent.turn_context._should_idle_compact`` — the pure predicate that
decides whether a session resuming after an idle gap should compact up front.
The predicate is intentionally side-effect-free so the policy can be verified
without constructing a live agent or DB.

Also covers the cache-aware skip: idle compaction only throws away a warm
prompt cache, so a route whose provider cache never expires (Codex OAuth
accounts, ``api_mode == "codex_responses"``) must never idle-compact
(parity with oh-my-pi's ``promptCachePersistent`` policy gate).
"""

from types import SimpleNamespace

from agent.turn_context import _should_idle_compact
from agent import turn_context_compaction as tcc


def _decide(**overrides):
    """Call the predicate with sensible defaults (idle + large context => fire)."""
    kwargs: dict = dict(
        enabled=True,
        idle_after_seconds=1800,
        idle_gap_seconds=3600.0,
        tokens=100_000,
        floor_tokens=40_000,
        cooldown_active=False,
    )
    kwargs.update(overrides)
    return _should_idle_compact(**kwargs)


class TestShouldIdleCompact:

    def test_disabled_when_idle_after_zero(self):
        # 0 is the documented "off" value — must never fire regardless of gap.
        assert _decide(idle_after_seconds=0, idle_gap_seconds=10_000.0) is False


    def test_disabled_when_compression_off(self):
        assert _decide(enabled=False) is False




    def test_fires_just_above_floor(self):
        assert _decide(tokens=40_001, floor_tokens=40_000) is True


class TestPostCompactionFloor:
    """The floor also honours what the previous pass actually produced (#97239).

    ``floor_tokens`` is the theoretical target (threshold × target_ratio); a
    real pass lands well above it because the system prompt, the tool schemas
    and the protected head/tail are incompressible. Without this, an already
    compacted session re-summarises itself on every idle resume forever.
    """

    def test_unrecorded_last_compaction_keeps_original_floor(self):
        # 0 = nothing compacted yet (or state reset) — original semantics.
        assert _decide(tokens=40_001, floor_tokens=40_000,
                       last_compaction_tokens=0) is True

    def test_skips_when_transcript_has_not_grown_since_last_compaction(self):
        # Previous pass produced 44,000; the transcript is still ~that size.
        assert _decide(tokens=44_100, floor_tokens=40_000,
                       last_compaction_tokens=44_000) is False


class TestPromptCachePersistentSkip:
    """Idle compaction discards a warm prompt cache; a route whose cache never
    expires must never be idle-compacted (inverted cost/benefit)."""

    def test_persistent_cache_never_idle_compacts(self):
        # Every other gate says "fire", but the persistent-cache gate wins.
        assert _decide(prompt_cache_persistent=True) is False

    def test_persistent_cache_skip_ignores_size(self):
        # Even a huge transcript must not fire: reclaiming tokens costs the
        # whole never-expiring prefix cache.
        assert _decide(tokens=500_000, floor_tokens=40_000,
                       prompt_cache_persistent=True) is False

    def test_non_persistent_cache_keeps_original_semantics(self):
        assert _decide(prompt_cache_persistent=False) is True


class TestPromptCacheNeverExpiresHelper:
    """The caller-side helper: which routes count as never-expiring."""

    def test_codex_responses_is_persistent(self):
        agent = SimpleNamespace(api_mode="codex_responses")
        assert tcc._prompt_cache_never_expires(agent) is True

    def test_chat_completions_is_not_persistent(self):
        agent = SimpleNamespace(api_mode="chat_completions")
        assert tcc._prompt_cache_never_expires(agent) is False

    def test_missing_attribute_is_not_persistent(self):
        assert tcc._prompt_cache_never_expires(SimpleNamespace()) is False


class TestIdleCompactionCallerHonoursCacheSkip:
    """End-to-end through the caller: a codex_responses agent whose gap/size
    gates all pass must not call ``_compress_context``."""

    @staticmethod
    def _agent(api_mode: str, monkeypatch):
        calls = []

        def _fake_compress(messages, system_message, approx_tokens=None, task_id=None, trigger=None):
            calls.append(trigger)
            return messages, system_message  # same list => "skipped" per caller contract

        compressor = SimpleNamespace(
            threshold_tokens=80_000,
            summary_target_ratio=0.5,
            awaiting_real_usage_after_compression=False,
            last_compression_rough_tokens=0,
            get_active_compression_failure_cooldown=lambda: None,
        )
        agent = SimpleNamespace(
            api_mode=api_mode,
            compression_enabled=True,
            compression_idle_compact_after_seconds=60,
            _last_activity_ts=0.0,  # epoch => gap enormous
            context_compressor=compressor,
            model="test-model",
            session_id="s-test",
            _emit_status=lambda msg: None,
            _compress_context=_fake_compress,
        )

        from agent import turn_context as tc
        monkeypatch.setattr(tc, "_preflight_request_tokens", lambda a, m, s: 100_000)
        monkeypatch.setattr(tcc, "automatic_compaction_status_message",
                            lambda *a, **k: None)
        monkeypatch.setattr(tcc, "conversation_history_after_compression",
                            lambda *a, **k: None)

        out = SimpleNamespace(
            messages=[{"role": "user", "content": "hi"}],
            active_system_prompt="sys",
            conversation_history=None,
            current_turn_user_idx=0,
        )
        return agent, out, calls

    def test_codex_responses_agent_skips_idle_compression(self, monkeypatch):
        agent, out, calls = self._agent("codex_responses", monkeypatch)
        tcc._idle_compaction(agent, out, "sys", "hi", "task-1")
        assert calls == []

    def test_chat_completions_agent_still_idle_compacts(self, monkeypatch):
        agent, out, calls = self._agent("chat_completions", monkeypatch)
        tcc._idle_compaction(agent, out, "sys", "hi", "task-1")
        assert calls == ["idle"]
