"""Diminishing-returns continuation budget — thread-safe per-turn tracker.

Port of Claude Code's ``query/tokenBudget.ts``: stop nudging the model forward
when continuations stop producing new tokens.  ``run_conversation`` instantiates
one tracker per user turn; the three nudge sites (length-continuation, codex
ack-continuation, empty-response nudge) call :meth:`record_continuation` and
stop the loop gracefully when it returns True.
"""

from __future__ import annotations

import threading
import time


class ContinuationBudgetTracker:
    """Tracks continuation count and per-continuation token deltas for one turn.

    Stop rule (Claude Code tokenBudget.ts semantics):
      ``continuation_count >= min_continuations``
      AND the last two deltas are each ``< diminishing_threshold``
      → diminishing returns; the caller should stop the loop gracefully.

    The check runs BEFORE the counter is incremented, so with the default
    ``min_continuations=3`` the earliest stop fires on the 4th nudge (after
    three recorded continuations and two consecutive small deltas).
    """

    def __init__(self, min_continuations: int = 3, diminishing_threshold: int = 500):
        self.min_continuations = min_continuations
        self.diminishing_threshold = diminishing_threshold
        self.continuation_count = 0
        self.last_delta_tokens = 0
        self.last_global_turn_tokens = 0
        self.started_at = time.monotonic()
        self._lock = threading.Lock()

    def record_continuation(self, global_turn_tokens: int) -> bool:
        """Record a continuation nudge. Returns True when the loop MUST stop."""
        with self._lock:
            delta = global_turn_tokens - self.last_global_turn_tokens
            is_diminishing = (
                self.continuation_count >= self.min_continuations
                and delta < self.diminishing_threshold
                and self.last_delta_tokens < self.diminishing_threshold
            )
            if is_diminishing:
                return True
            self.continuation_count += 1
            self.last_delta_tokens = delta
            self.last_global_turn_tokens = global_turn_tokens
            return False


__all__ = ["ContinuationBudgetTracker"]
