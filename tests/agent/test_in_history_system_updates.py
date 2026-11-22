"""In-history system updates (DSH A1 parity): route gate, bounded queue, tail rows.

Pins the contract: config opt-in AND chat_completions only; disable/other routes keep
deferring; one standalone system row per update at the tail (prefix untouched); the
oldest update is dropped past the cap; a route that turns incapable mid-turn keeps
the queue for later rather than silently losing it.
"""

from types import SimpleNamespace

import pytest

import agent.in_history_system_updates as ihsu


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(ihsu, "_in_history_enabled", lambda: True)
    yield


def _agent(api_mode: str = "chat_completions"):
    return SimpleNamespace(api_mode=api_mode)


class TestRouteGate:

    def test_disabled_config_keeps_deferral(self, monkeypatch):
        monkeypatch.setattr(ihsu, "_in_history_enabled", lambda: False)
        agent = _agent()
        assert ihsu.route_supports_in_history_updates(agent) is False
        assert ihsu.queue_in_history_update(agent, "x") is False

    @pytest.mark.parametrize("api_mode", ["codex_responses", "anthropic_messages"])
    def test_non_chat_completions_modes_keep_deferral(self, enabled, api_mode):
        assert ihsu.route_supports_in_history_updates(_agent(api_mode)) is False

    def test_enabled_chat_completions_route_is_capable(self, enabled):
        assert ihsu.route_supports_in_history_updates(_agent()) is True
        assert ihsu.route_supports_in_history_updates(_agent("chat_completions")) is True


class TestQueueAndInject:

    def test_queue_then_inject_appends_one_system_row_at_tail(self, enabled):
        agent = _agent()
        messages = [{"role": "user", "content": "hi"}]
        assert ihsu.queue_in_history_update(agent, "Skill 'foo' installed and active.") is True
        appended = ihsu.inject_pending_in_history_updates(agent, messages)
        assert appended == 1
        assert messages[-1]["role"] == "system"
        assert "Skill 'foo' installed and active." in messages[-1]["content"]
        assert messages[0] == {"role": "user", "content": "hi"}  # prefix untouched
        assert ihsu.drain_pending_in_history_updates(agent) == []  # consumed

    def test_update_rows_never_merge(self, enabled):
        agent = _agent()
        messages = []
        ihsu.queue_in_history_update(agent, "first")
        ihsu.queue_in_history_update(agent, "second")
        assert ihsu.inject_pending_in_history_updates(agent, messages) == 2
        assert [m["role"] for m in messages] == ["system", "system"]
        assert "first" in messages[0]["content"] and "second" in messages[1]["content"]

    def test_cap_drops_oldest(self, enabled):
        agent = _agent()
        for i in range(ihsu.MAX_PENDING_UPDATES + 2):
            ihsu.queue_in_history_update(agent, f"u{i}")
        pending = ihsu.drain_pending_in_history_updates(agent)
        assert len(pending) == ihsu.MAX_PENDING_UPDATES
        assert pending[0] == "u2" and pending[-1] == f"u{ihsu.MAX_PENDING_UPDATES + 1}"

    def test_incapable_route_keeps_queue_intact(self, enabled):
        agent = _agent()
        ihsu.queue_in_history_update(agent, "pending update")
        agent.api_mode = "codex_responses"  # route switched mid-turn
        assert ihsu.inject_pending_in_history_updates(agent, []) == 0
        assert ihsu.drain_pending_in_history_updates(agent) == ["pending update"]

    def test_empty_text_is_never_queued(self, enabled):
        agent = _agent()
        assert ihsu.queue_in_history_update(agent, "  ") is False


class TestTurnLoopSeam:
    """prepare_iteration drains the queue beside the /steer drain (same cache-safe contract)."""

    def test_prepare_iteration_appends_pending_system_update(self, enabled):
        from agent.turn_iteration_prep import prepare_iteration

        agent = SimpleNamespace(
            api_mode="chat_completions",
            _nous_wire_pending=None,
            step_callback=None,
            _skill_nudge_interval=0,
            valid_tool_names=[],
            _adopt_nous_key_before_expiry=lambda: None,
            _drain_pending_steer=lambda: None,
            run_budget_seconds=None,
            logger=None,
        )
        ihsu.queue_in_history_update(agent, "Memory reloaded: 3 new facts available.")
        messages = [{"role": "user", "content": "hi"}]
        try:
            prepare_iteration(agent, messages=messages, api_call_count=1)
        except Exception:
            # Only the append effect is under contract here; later phases have their own tests.
            pass
        assert any(m.get("role") == "system" and "Memory reloaded" in m.get("content", "")
                   for m in messages), messages
        assert ihsu.drain_pending_in_history_updates(agent) == []
