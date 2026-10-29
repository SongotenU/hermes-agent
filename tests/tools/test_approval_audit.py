"""Durable approval audit pair (state.db / approval_events).

Ported from DeepSeek Harness' user-approval seam (``approval/asked`` +
``approval/decided``): every human approval ask is durably paired with its decision
under one fresh ``request_id``, carrying the turn/tool-call/session correlation ids
the approval context already binds. Log-only — never part of the model transcript.

Writes are best-effort by design: a failed append never blocks or alters a decision
(the approval flow is safety-critical; audit observability is not).
"""

from contextlib import contextmanager

import pytest

import tools.approval as approval_module
from tools import approval_context, approval_prompt


def _rows(session_id):
    from hermes_state_approvals import list_approval_events
    return list_approval_events(session_id)


@contextmanager
def _bound(session_id):
    tokens = approval_context.set_current_observability_context(
        turn_id="turn-1", tool_call_id="call-1", session_id=session_id)
    try:
        yield
    finally:
        approval_context.reset_current_observability_context(tokens)


@pytest.fixture
def cli_env(monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", "audit-test")
    for var in ("HERMES_EXEC_ASK", "HERMES_CRON_SESSION", "HERMES_GATEWAY_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    approval_module.clear_session("audit-test")
    approval_module._permanent_approved.clear()
    key_token = approval_context.set_current_session_key("audit-test")
    yield
    approval_context.reset_current_session_key(key_token)


def _prompt(monkeypatch, choice):
    monkeypatch.setattr(approval_module, "prompt_dangerous_approval", lambda *a, **k: choice)
    monkeypatch.setattr(approval_prompt, "prompt_dangerous_approval", lambda *a, **k: choice)


class TestApprovalAuditPair:
    def test_cli_approve_records_paired_asked_and_decided(self, cli_env, monkeypatch):
        _prompt(monkeypatch, "once")
        with _bound("sess-approve"):
            result = approval_module.check_dangerous_command("rm -rf /tmp/audit-a", "local")

        assert result["approved"] is True
        rows = _rows("sess-approve")
        assert [r["event"] for r in rows] == ["asked", "decided"]
        asked, decided = rows
        assert asked["request_id"] == decided["request_id"]
        assert asked["surface"] == "cli"
        assert asked["session_id"] == "sess-approve"
        assert asked["turn_id"] == "turn-1"
        assert asked["tool_call_id"] == "call-1"
        assert asked["pattern_key"] == "delete in root path"
        assert decided["outcome"] == "approved"
        assert decided["choice"] == "once"

    def test_cli_deny_records_denied_outcome(self, cli_env, monkeypatch):
        _prompt(monkeypatch, "deny")
        with _bound("sess-deny"):
            result = approval_module.check_dangerous_command("rm -rf /tmp/audit-b", "local")

        assert result["approved"] is False
        rows = _rows("sess-deny")
        assert [r["event"] for r in rows] == ["asked", "decided"]
        assert rows[1]["outcome"] == "denied"

    def test_cli_timeout_records_timeout_outcome(self, cli_env, monkeypatch):
        _prompt(monkeypatch, "timeout")
        with _bound("sess-timeout"):
            result = approval_module.check_dangerous_command("rm -rf /tmp/audit-c", "local")

        assert result["approved"] is False
        rows = _rows("sess-timeout")
        assert rows[1]["outcome"] == "timeout"

    def test_gateway_wait_records_gateway_surface(self, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_KEY", "audit-gw")
        monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
        for var in ("HERMES_INTERACTIVE", "HERMES_EXEC_ASK", "HERMES_CRON_SESSION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
        approval_module.clear_session("audit-gw")
        approval_module._permanent_approved.clear()
        monkeypatch.setattr(approval_module, "_gateway_notify_cb", lambda *_: (lambda data: None))
        monkeypatch.setattr(
            approval_module, "_await_gateway_decision",
            lambda *a, **k: {"resolved": True, "choice": "once"},
        )
        with _bound("sess-gateway"):
            result = approval_module.check_dangerous_command("rm -rf /tmp/audit-d", "local")

        assert result["approved"] is True
        rows = _rows("sess-gateway")
        assert [r["event"] for r in rows] == ["asked", "decided"]
        assert rows[0]["surface"] == "gateway"
        assert rows[1]["outcome"] == "approved"

    def test_auto_approved_path_records_nothing(self, cli_env, monkeypatch):
        approval_module.approve_session("audit-test", "delete in root path")
        with _bound("sess-auto"):
            result = approval_module.check_dangerous_command("rm -rf /tmp/audit-e", "local")

        assert result["approved"] is True
        assert _rows("sess-auto") == []

    def test_audit_write_failure_never_blocks_the_decision(self, cli_env, monkeypatch):
        _prompt(monkeypatch, "once")

        def boom(**kwargs):
            raise RuntimeError("audit store down")

        monkeypatch.setattr("hermes_state_approvals.record_approval_event", boom)
        with _bound("sess-fail"):
            result = approval_module.check_dangerous_command("rm -rf /tmp/audit-f", "local")

        assert result["approved"] is True  # decision unaffected
