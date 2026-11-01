"""Advisor severity tiers (B11): guard, channel routing, stop-gate continuation.

Pins OMP-parity semantics: NFKC severity-aware dedupe, 4 non-blocker budget per
evaluation, nit→aside / concern+blocker→steer / interrupt→preserve, and the stop
gate's ONE continuation anchored per turn (messages-list identity) fed by the
built-in todo producer or an emitted final-answer finding.
"""

from types import SimpleNamespace

import pytest

import agent.advisor_notes as adv
from agent.advisor_notes import (
    AdvisorBus,
    EmissionGuard,
    Severity,
    advisor_stop_nudge,
    emit_advisor_finding,
    resolve_delivery_channel,
)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(adv, "_agent_config", dict)


class TestEmissionGuard:

    def test_duplicate_suppressed(self):
        guard = EmissionGuard()
        assert guard.allow(Severity.CONCERN, "Fix the flaky test")
        assert guard.allow(Severity.CONCERN, "fix the   flaky test") is False  # normalized dup

    def test_higher_severity_reraise_allowed(self):
        guard = EmissionGuard()
        assert guard.allow(Severity.CONCERN, "same finding")
        assert guard.allow(Severity.BLOCKER, "same finding") is True
        assert guard.allow(Severity.CONCERN, "same finding") is False  # downgrades blocked

    def test_non_blocker_budget_four_per_update(self):
        guard = EmissionGuard()
        allowed = [guard.allow(Severity.NIT, f"nit {i}") for i in range(5)]
        assert allowed == [True, True, True, True, False]
        guard.begin_update("next-turn")
        assert guard.allow(Severity.NIT, "nit 5") is True

    def test_blockers_exempt_from_budget(self):
        guard = EmissionGuard()
        for i in range(4):
            assert guard.allow(Severity.NIT, f"nit {i}")
        assert guard.allow(Severity.BLOCKER, "blocker survives budget") is True

    def test_nfkc_equivalence(self):
        guard = EmissionGuard()
        assert guard.allow(Severity.CONCERN, "ｆｉｘ test")          # full-width
        assert guard.allow(Severity.CONCERN, "fix test") is False


class TestChannelResolution:

    def _cfg(self, monkeypatch, continue_severity="concern", on_todos=True):
        monkeypatch.setattr(adv, "_agent_config", lambda: {
            "advisor_continue_severity": continue_severity,
            "advisor_continue_on_todos": on_todos,
        })

    def test_nit_always_aside(self):
        assert resolve_delivery_channel(Severity.NIT, final_answer_reached=True) == "aside"
        assert resolve_delivery_channel(Severity.NIT, final_answer_reached=False) == "aside"

    def test_concern_steers_mid_turn_and_at_final(self):
        assert resolve_delivery_channel(Severity.CONCERN, final_answer_reached=False) == "steer"
        assert resolve_delivery_channel(Severity.CONCERN, final_answer_reached=True) == "steer"

    def test_interrupt_preserves(self):
        assert resolve_delivery_channel(
            Severity.BLOCKER, final_answer_reached=True, user_interrupted=True
        ) == "preserve"

    def test_continue_severity_blocker_downgrades_concern_at_final(self, monkeypatch):
        self._cfg(monkeypatch, continue_severity="blocker")
        assert resolve_delivery_channel(Severity.CONCERN, final_answer_reached=True) == "aside"
        assert resolve_delivery_channel(Severity.BLOCKER, final_answer_reached=True) == "steer"


class TestBus:

    def test_steer_finding_lands_in_pending(self, monkeypatch):
        agent = SimpleNamespace()
        emit_advisor_finding(agent, "concern", "reply is missing citations",
                             final_answer_reached=False)
        bus = adv.advisor_bus(agent)
        assert [text for _, text in bus.pending] == ["reply is missing citations"]

    def test_aside_finding_uses_diagnostic_callback(self):
        shown = []
        agent = SimpleNamespace(_emit_diagnostic_status=shown.append)
        emit_advisor_finding(agent, "nit", "prefer shorter lines", final_answer_reached=False)
        assert shown and "prefer shorter lines" in shown[0]
        assert adv.advisor_bus(agent).pending == []


class TestStopGateNudge:

    def _todos(self):
        from tools.todo_tool import TodoStore

        store = TodoStore()
        store.write([
            {"id": "1", "content": "run the migration", "status": "pending"},
            {"id": "2", "content": "ship the report", "status": "in_progress"},
            {"id": "3", "content": "done thing", "status": "completed"},
        ])
        return store

    def test_actionable_todos_raise_one_continuation(self):
        agent = SimpleNamespace(_todo_store=self._todos())
        messages = [{"role": "user", "content": "go"}]
        nudge = advisor_stop_nudge(agent, messages)
        assert nudge and "run the migration" in nudge
        # One per turn: the same messages list never nudges twice.
        assert advisor_stop_nudge(agent, messages) is None

    def test_completed_only_todos_do_not_nudge(self):
        from tools.todo_tool import TodoStore

        store = TodoStore()
        store.write([{"id": "1", "content": "done", "status": "completed"}])
        agent = SimpleNamespace(_todo_store=store)
        assert advisor_stop_nudge(agent, [{"role": "user", "content": "go"}]) is None

    def test_config_disables_todo_gate(self, monkeypatch):
        monkeypatch.setattr(adv, "_agent_config", lambda: {"advisor_continue_on_todos": False})
        agent = SimpleNamespace(_todo_store=self._todos())
        assert advisor_stop_nudge(agent, [{"role": "user", "content": "go"}]) is None

    def test_interrupt_suppresses_nudge(self):
        agent = SimpleNamespace(_todo_store=self._todos(), _interrupt_requested=True)
        assert advisor_stop_nudge(agent, [{"role": "user", "content": "go"}]) is None

    def test_emitted_final_answer_finding_nudges_once(self):
        agent = SimpleNamespace()
        emit_advisor_finding(agent, "blocker", "the answer contradicts the diff",
                             final_answer_reached=True)
        messages = [{"role": "user", "content": "go"}]
        nudge = advisor_stop_nudge(agent, messages)
        assert nudge and "contradicts the diff" in nudge
        assert advisor_stop_nudge(agent, messages) is None

    def test_emitted_nit_never_nudges(self):
        agent = SimpleNamespace()
        emit_advisor_finding(agent, "nit", "cosmetic point", final_answer_reached=True)
        assert advisor_stop_nudge(agent, [{"role": "user", "content": "go"}]) is None

    def test_same_todos_never_renudge_next_turn(self):
        agent = SimpleNamespace(_todo_store=self._todos())
        assert advisor_stop_nudge(agent, [{"role": "user", "content": "go"}])
        # A later turn is a NEW list: the anchor clears, but the guard's dedupe holds.
        assert advisor_stop_nudge(agent, [{"role": "user", "content": "go"}]) is None
