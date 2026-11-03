"""Advisor severity tiers (OMP parity): nit/concern/blocker routing + emission guard.

OMP's advisor reviews agent deltas and routes findings by severity: ``nit`` → aside
(user-visible note, never interrupts), ``concern``/``blocker`` → the steer channel.
A finding raised at/after the terminal answer wakes exactly ONE continuation when its
severity reaches ``advisor.continue_severity`` (default ``concern``); an explicit user
interrupt suppresses wakes (``preserve``). ``EmissionGuard`` dedupes severity-aware
(NFKC-normalized; a re-raise at a higher severity is allowed) and bounds non-blocker
noise (4 per update).

Hermes invariants kept: findings are delivered as user-role rows appended after the
answer row (the stop-gate contract), never spliced into cached history. The built-in
producer is the todo list: a final answer with actionable todos raises one concern
(config ``agent.advisor_continue_on_todos``, default on) so the agent finishes its
own list instead of stopping mid-plan.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from enum import IntEnum
from typing import Any, Optional

logger = logging.getLogger(__name__)

_NON_BLOCKER_BUDGET = 4
_MAX_CONTINUATIONS = 1


class Severity(IntEnum):
    NIT = 1
    CONCERN = 2
    BLOCKER = 3


_SEVERITY_NAMES = {"nit": Severity.NIT, "concern": Severity.CONCERN, "blocker": Severity.BLOCKER}


def parse_severity(value: Any, default: Severity = Severity.CONCERN) -> Severity:
    if isinstance(value, Severity):
        return value
    if isinstance(value, int) and value in (1, 2, 3):
        return Severity(value)
    return _SEVERITY_NAMES.get(str(value or "").strip().lower(), default)


def _agent_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        return (load_config_readonly() or {}).get("agent") or {}
    except Exception:
        logger.debug("advisor config unreadable; using defaults", exc_info=True)
        return {}


def continue_severity() -> Severity:
    """``agent.advisor_continue_severity`` (nit|concern|blocker), default concern."""
    return parse_severity(_agent_config().get("advisor_continue_severity"), Severity.CONCERN)


def continue_on_todos() -> bool:
    return bool(_agent_config().get("advisor_continue_on_todos", True))


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", " ", folded).strip().casefold()


class EmissionGuard:
    """Severity-aware dedupe + non-blocker budget, scoped to one update (turn)."""

    def __init__(self) -> None:
        self._seen: dict[str, Severity] = {}
        self._update_id: Optional[str] = None
        self._non_blocker_count = 0

    def begin_update(self, update_id: str) -> None:
        if update_id != self._update_id:
            self._update_id = update_id
            self._non_blocker_count = 0

    def allow(self, severity: Severity, text: str) -> bool:
        """True when this finding may be delivered: new, or a higher-severity re-raise,
        within the non-blocker budget."""
        norm = _normalize(text)
        if not norm:
            return False
        previous = self._seen.get(norm)
        if previous is not None and severity <= previous:
            return False  # duplicate, or a downgrade of the same finding
        if severity < Severity.BLOCKER:
            if self._non_blocker_count >= _NON_BLOCKER_BUDGET:
                return False
            self._non_blocker_count += 1
        self._seen[norm] = severity
        return True


def resolve_delivery_channel(
    severity: Severity, *, final_answer_reached: bool, user_interrupted: bool = False,
) -> str:
    """OMP routing: ``aside`` (passive note), ``steer`` (drive the turn), ``preserve``
    (hold for a later turn boundary — an explicit interrupt is never fought)."""
    if severity is Severity.NIT:
        return "aside"
    if user_interrupted:
        return "preserve"
    if final_answer_reached and severity < continue_severity():
        return "aside"
    return "steer"


class AdvisorBus:
    """Per-agent findings bus: guard → channel → pending steers / delivered asides."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self.guard = EmissionGuard()
        self.pending: list[tuple[Severity, str]] = []
        self._continuations = 0

    def emit(
        self, severity: Severity, text: str, *, final_answer_reached: bool,
        user_interrupted: bool = False, update_id: str = "",
    ) -> Optional[str]:
        self.guard.begin_update(update_id or str(id(self._agent)))
        if not self.guard.allow(severity, text):
            return None
        channel = resolve_delivery_channel(
            severity, final_answer_reached=final_answer_reached, user_interrupted=user_interrupted,
        )
        if channel == "steer":
            self.pending.append((severity, text))
        elif channel == "aside":
            logger.debug("advisor aside (%s): %s", severity.name.lower(), text[:160])
            emit = getattr(self._agent, "_emit_diagnostic_status", None)
            if callable(emit):
                try:
                    emit(f"💭 Advisor ({severity.name.lower()}): {text}")
                except Exception:
                    logger.debug("advisor aside display failed", exc_info=True)
        # preserve: dropped for this turn; the finding reached no consumer on purpose.
        return channel

    def take_pending(self) -> list[tuple[Severity, str]]:
        pending, self.pending = self.pending, []
        return pending

    def consume_continuation(self) -> bool:
        """True exactly ``_MAX_CONTINUATIONS`` times per turn."""
        if self._continuations >= _MAX_CONTINUATIONS:
            return False
        self._continuations += 1
        return True

    def reset_turn(self) -> None:
        self._continuations = 0
        self.pending = []


def advisor_bus(agent: Any) -> AdvisorBus:
    bus = getattr(agent, "_advisor_bus", None)
    if not isinstance(bus, AdvisorBus):
        bus = AdvisorBus(agent)
        agent._advisor_bus = bus
    return bus


def emit_advisor_finding(agent: Any, severity: Any, text: str, **kwargs: Any) -> Optional[str]:
    """Public producer API (MoA/plugins/verify hooks): route one finding. Returns the
    delivery channel or None when the guard suppressed it."""
    return advisor_bus(agent).emit(parse_severity(severity), text, **kwargs)


def actionable_todos(agent: Any) -> Optional[str]:
    """The todo list's actionable block (pending/in_progress only), or None."""
    store = getattr(agent, "_todo_store", None)
    if store is None:
        return None
    try:
        return store.format_for_injection()
    except Exception:
        logger.debug("todo store render failed", exc_info=True)
        return None


def advisor_stop_nudge(agent: Any, messages: Any = None) -> Optional[str]:
    """Stop gate: deliver one continuation when the advisor has a steer-worthy finding.

    Sources: findings emitted at final-answer time (MoA/plugins via
    ``emit_advisor_finding(final_answer_reached=True)``), then the built-in todo
    producer. A user interrupt suppresses the nudge; the continuation cap is anchored
    on the messages list identity — one nudge per turn, loop-safe by construction.
    """
    if getattr(agent, "_interrupt_requested", False):
        return None
    if messages is not None and getattr(agent, "_advisor_nudge_anchor", None) is messages:
        return None  # already nudged this turn
    bus = advisor_bus(agent)
    invocation = getattr(agent, "_advisor_gate_invocations", 0) + 1
    agent._advisor_gate_invocations = invocation
    bus.guard.begin_update(f"stop-gate-{invocation}")  # non-blocker budget per evaluation
    if continue_on_todos() and not bus.pending:
        block = actionable_todos(agent)
        if block:
            bus.emit(
                Severity.CONCERN,
                "The todo list still has actionable work; finish it (or explicitly record why it "
                "is deferred) before the final answer:\n" + block,
                final_answer_reached=True,
            )
    pending = bus.take_pending()
    if not pending:
        return None
    if messages is not None:
        agent._advisor_nudge_anchor = messages
    lines = "\n".join(f"- [{severity.name.lower()}] {text}" for severity, text in pending)
    return (
        "Advisor notes raised at the final answer — address them, then answer again:\n" + lines
    )
