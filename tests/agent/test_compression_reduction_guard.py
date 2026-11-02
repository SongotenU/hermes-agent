"""Reduction guard for context compression (ported from xai-org/grok-build,
``intra_compaction`` ``max_reduction_ratio``).

A summary pass that does not actually shrink the window it replaces is pure
loss: it burns an auxiliary summary call, rewrites history, and reclaims
(almost) nothing. Grok rejects the pass post-hoc when the result keeps more
than ``max_reduction_ratio`` (0.8 => at least 20% must be reclaimed). Hermes
measures that ratio on the COMPRESS WINDOW (head/tail are protected by design
and must not dilute it) and keeps the lossless transcript instead, letting the
existing ineffective-compression machinery carry the failure.
"""

from agent.context_compressor_reduction_guard import (
    MIN_COMPRESSION_REDUCTION_RATIO,
    reject_non_reducing_compression,
)


def _msg(text: str) -> dict:
    return {"role": "user", "content": text}


class TestReductionGuard:

    def test_rejects_when_summary_is_as_large_as_its_window(self):
        # ~16k chars (~4k rough tokens) window; summary keeps ~95%.
        window = [_msg("x" * 16_000)]
        summary = "y" * 15_200
        telemetry: dict = {}
        assert reject_non_reducing_compression(telemetry, window, summary) is True
        assert telemetry.get("reduction_guard_rejected") is True
        assert telemetry["reduction_guard_pre_tokens"] > telemetry["reduction_guard_post_tokens"]

    def test_accepts_when_summary_shrinks_the_window(self):
        window = [_msg("x" * 16_000)]
        summary = "y" * 4_000  # 75% reclaimed
        telemetry: dict = {}
        assert reject_non_reducing_compression(telemetry, window, summary) is False
        assert "reduction_guard_rejected" not in telemetry

    def test_never_rejects_windows_below_the_noise_floor(self):
        # Below the measurement floor rough estimates are noise; accept anything.
        window = [_msg("x" * 7_600)]  # ~1.9k rough tokens < 2k floor
        telemetry: dict = {}
        assert reject_non_reducing_compression(telemetry, window, "x" * 7_400) is False
        assert "reduction_guard_rejected" not in telemetry

    def test_empty_window_is_not_rejected(self):
        telemetry: dict = {}
        assert reject_non_reducing_compression(telemetry, [], "") is False

    def test_ratio_floor_matches_grok_parity(self):
        # Grok keeps max_reduction_ratio = 0.8; the complement is a 20% floor.
        assert MIN_COMPRESSION_REDUCTION_RATIO == 0.2
