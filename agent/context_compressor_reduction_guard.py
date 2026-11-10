"""Post-hoc reduction guard for context compression (Grok-build parity).

Ported design (not code) from xai-org/grok-build ``xai-grok-compaction``:
``intra_compaction/compact.rs`` rejects a compaction whose result keeps more
than ``max_reduction_ratio`` of the input (default 0.8 — the compacted span
must actually shrink by at least 20%).

Measurement scope — the COMPRESS WINDOW, not the whole transcript. Hermes
protects a head and a tail by design (system prompt, recent turns, pending
images), so a ratio over the full canonical transcript is diluted by bytes the
pass may never touch (a real 16% reclaim of the total can be a 95% reclaim of
the window). The guard therefore asks the Grok question about the span the
summary actually replaces: does the summary cost less than 80% of the window it
summarizes? A summary as large as its input burned an auxiliary call and
rewrote history for nothing — reject post-hoc, keep the transcript lossless,
and let the existing ineffective-compression counters / anti-thrash ladder
handle the consequences on the next pass.

Scope: AUTOMATIC summary passes only (auto-compress callers pass ``force=False``).
The caller skips the guard for the deterministic fallback (fat-tail / pre-LLM
skip), whose contract is "make any progress", not "shrink a ratio", and for
explicit manual ``/compress`` (``force=True``): the user asked for the pass, so
honoring it beats a ratio check.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The compacted window must shrink by at least this fraction for a pass to count
# (Grok ``max_reduction_ratio = 0.8`` complement).
MIN_COMPRESSION_REDUCTION_RATIO = 0.2

# Below this rough-token floor the message-only estimate is noise (short fixtures are
# routinely off by hundreds of tokens), and rejecting a small window would break flows
# whose summaries are intentionally near-window-sized. Production auto-compaction
# windows run tens of thousands of rough tokens; only pathological summaries reach here.
MIN_REDUCTION_GUARD_TOKENS = 2_000


def reject_non_reducing_compression(
    telemetry: dict[str, Any], window_messages: list[Any], summary: str,
) -> bool:
    """Return True when ``summary`` failed to shrink the ``window_messages`` it
    replaces by at least ``MIN_COMPRESSION_REDUCTION_RATIO``; the caller keeps the
    lossless transcript. Records telemetry flags on every comparable pass."""
    from agent.model_metadata import estimate_messages_tokens_rough

    window_tokens = estimate_messages_tokens_rough(window_messages)
    if window_tokens < MIN_REDUCTION_GUARD_TOKENS:
        return False
    summary_tokens = estimate_messages_tokens_rough([{"role": "user", "content": summary or ""}])
    telemetry["reduction_guard_pre_tokens"] = window_tokens
    telemetry["reduction_guard_post_tokens"] = summary_tokens
    reclaimed = window_tokens - summary_tokens
    if reclaimed < window_tokens * MIN_COMPRESSION_REDUCTION_RATIO:
        telemetry["reduction_guard_rejected"] = True
        logger.warning(
            "Compression rejected by reduction guard: window ~%d -> summary ~%d tokens "
            "(<%.0f%% reclaimed); keeping original transcript",
            window_tokens,
            summary_tokens,
            MIN_COMPRESSION_REDUCTION_RATIO * 100,
        )
        return True
    return False
