"""Summary-failure classification for the context compressor.

Sibling of ``agent/context_compressor.py``: the RuntimeError markers a failed summary call
raises, the transient-failure kinds and their classifier, and the cooldown ladder — all pure
helpers over an exception (no compressor state), so the facade imports them back at module
level. Split out of the compressor for the per-file size ratchet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agent.auxiliary_client import CODEX_STREAM_STALL_MARKER, _is_connection_error
from agent.error_classifier import FailoverReason, classify_api_error

# Marker for a length-stopped (PARTIAL) summary; the except-branch classifier keys
# on this exact substring, so keep raise sites and classifier in sync.
# RuntimeError marker raised when the summarizer's generation stopped on the output-token cap
# (``finish_reason == "length"``). A length stop means the summary text is PARTIAL — persisting it as a
# compaction checkpoint would silently truncate the conversation's memory and feed the cut-off text back
# into every subsequent iterative-update prompt. (Ported from earendil-works/pi#7048 / commit 97fa14e39.)
_TRUNCATED_SUMMARY_MARKER = "finish_reason=length"

# RuntimeError marker raised when a CLEANED summary is shorter than the seed floor below. A summary
# that short cannot have covered the compacted turns — a thinking model that emitted only a preamble
# ("Done.") would otherwise commit as the compaction checkpoint and silently wipe the compacted turns.
# Routes through the same main-model fallback + cooldown/abort ladder as a truncated summary; the
# classifier keys on this exact substring, so keep raise sites and classifier in sync.
# (Ported from xai-org/grok-build's is_degenerate_summary / MIN_SUMMARY_SEED_CHARS.)
_DEGENERATE_SUMMARY_MARKER = "degenerate summary"
# Cleaned-summary length floor, scaled by transcript size (grok-build uses a flat 500-char seed
# floor for whole-session full-replace compaction; Hermes also compacts small transcripts, where a
# flat floor would reject legitimate terse summaries): floor = min(cap, input // 20) — a summary
# under ~5% of its transcript's chars cannot have covered it. No absolute minimum: a tiny
# transcript's floor shrinks with it. Below this the summary is degenerate — retry/abort, never
# commit.
_MIN_SUMMARY_SEED_CHARS = 500


def degenerate_summary_floor(input_chars: int) -> int:
    """The size-scaled minimum cleaned-summary length for a transcript of *input_chars* chars."""
    return min(_MIN_SUMMARY_SEED_CHARS, input_chars // 20)


def check_degenerate_summary(summary: str, input_chars: int) -> None:
    """Raise the degenerate-marker RuntimeError when a cleaned summary is below the floor.

    Degenerate-summary gate (ported from xai-org/grok-build's is_degenerate_summary, with the
    floor scaled down for small transcripts): a CLEANED summary shorter than the floor cannot
    have covered the compacted turns — a thinking model that emitted only a preamble would
    otherwise commit as the compaction checkpoint and silently wipe them. Routes through the
    main-model fallback + cooldown/abort ladder like a truncated summary.
    """
    floor = degenerate_summary_floor(input_chars)
    if len(summary.strip()) < floor:
        raise RuntimeError(
            f"Context compression summary was degenerate ({_DEGENERATE_SUMMARY_MARKER}): "
            f"{len(summary.strip())} chars < {floor}-char floor for a {input_chars}-char transcript"
        )


def _exc_status_code(exc: Exception) -> Any:
    """HTTP status carried on the exception itself or on its ``response``."""
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


@dataclass
class _SummaryFailureKind:
    """Transient-failure classes of a summary call (several may hold at once)."""

    model_not_found: bool
    timeout: bool
    json_decode: bool
    streaming_closed: bool
    empty_content: bool
    truncated: bool
    degenerate: bool
    overloaded: bool

    def fallback_reason(self) -> str:
        """Reason string for the one-shot main-model retry log line, most specific first."""
        reasons = (
            (self.json_decode, "returned invalid JSON"), (self.truncated, "returned a truncated summary (output token cap)"),
            (self.degenerate, "returned a degenerate summary (too short to cover the compacted turns)"),
            (self.empty_content, "returned empty content"), (self.overloaded, "was overloaded"),
            (self.model_not_found, "unavailable"),
            (self.streaming_closed, "closed stream prematurely"), (self.timeout, "timed out"),
        )
        return next((reason for flagged, reason in reasons if flagged), "failed")


def _classify_summary_failure(e: Exception) -> _SummaryFailureKind:
    """Classify a summary-call exception by status code / message shape.

    A "refusal content" RuntimeError (prose or provider ``refusal`` field) deliberately rides the
    ``empty_content`` class — cooldown + main-model fallback + abort — so the "returned empty content"
    fallback log line is expected for refusals.
    """
    status = _exc_status_code(e)
    err = str(e).lower()
    # #124077: only the Codex aux stream guard's mid-stream stall is a retry-ladder timeout; real
    # transport timeouts and the guard's no-progress/hard-ceiling timeouts stay terminal
    # network failures (#29559/#94448).
    stall = isinstance(e, TimeoutError) and CODEX_STREAM_STALL_MARKER in str(e)
    return _SummaryFailureKind(
        # Permanent-looking error on a distinct summary model: fall back to main instead of cooldown.
        model_not_found=status in {404, 503}
        or any(m in err for m in ("model_not_found", "does not exist", "no available channel")),
        timeout=stall or status in {408, 429, 502, 504} or "timeout" in err or "timed out" in err,
        # Malformed/non-JSON bodies (HTML 502 as application/json) surface as JSONDecodeError or
        # APIResponseValidationError "expecting value"; treat as transient.
        json_decode=isinstance(e, json.JSONDecodeError) or "expecting value" in err,
        # httpx premature-close errors are transient; treat like a timeout, not a 60s cooldown.
        streaming_closed=_is_connection_error(e) and not stall,
        # HTTP 200 with empty body from a degraded provider, plus the sibling "no usable response"
        # shapes from _validate_llm_response.
        empty_content=isinstance(e, RuntimeError) and any(
            m in err for m in (
                "empty content", "refusal content", "llm returned none response", "llm returned invalid response",
            )
        ),
        # Truncated summary: one main-model retry, then ABORT preserving the session.
        truncated=isinstance(e, RuntimeError) and _TRUNCATED_SUMMARY_MARKER in err,
        # Degenerate summary (cleaned output below the seed floor): same ladder as truncated.
        degenerate=isinstance(e, RuntimeError) and _DEGENERATE_SUMMARY_MARKER in err,
        overloaded=classify_api_error(e).reason is FailoverReason.overloaded
        or any(marker in err for marker in ("overloaded", "at capacity", "over capacity")),
    )


# Timeouts escalate 60s -> 300s -> 900s: structural repeat offenders back off longer. Truncated summaries
# (finish_reason=length) walk the same rungs on their own counter: the output cap is deterministic for an
# unchanged route and prompt, so a flat 30s cooldown let every async-completion turn re-issue the same
# capped request after its per-turn attempt budget was refilled (#69637).
_TIMEOUT_COOLDOWN_LADDER = (60, 300, 900)


def _next_timeout_cooldown(compressor: Any, counter: str = "_consecutive_timeout_failures") -> int:
    """Bump ``compressor.<counter>`` and return the ladder rung for it.
    Module-level (not a method) so callers that bind a single real method onto a stub still exercise the ladder.
    ``counter`` stays separate per failure class: the timeout streak also arms the deterministic stall fallback
    (``_prior_timeout_failures``), which a truncation must not trigger."""
    n = getattr(compressor, counter, 0) + 1
    setattr(compressor, counter, n)
    return _TIMEOUT_COOLDOWN_LADDER[min(n, len(_TIMEOUT_COOLDOWN_LADDER)) - 1]
