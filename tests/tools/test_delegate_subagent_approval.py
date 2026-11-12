"""Subagent approvals in the parent's conversation: attribution and ``delegation.subagent_approval``.

Ported from T3 Code's permission model (subagent approval requests surface in the parent with the
subagent's name; port pin 6577c6b61851). Hermes behaviour under test:

- A delegated child's dangerous command that reaches the gateway approval queue carries the asking
  subagent's label (``[subagent: <goal>]`` in the card, ``origin`` in the payload); a session's own
  approvals stay unlabelled.
- ``delegation.subagent_approval`` decides BEFORE any prompt: ``deny``/``approve`` never notify the
  user; ``inherit``/unset keeps today's per-surface behaviour.
- The legacy ``delegation.subagent_auto_approve`` still selects the CLI worker callback when the
  policy is unset.
"""
import threading

import pytest

from tools import approval as mod
from tools import approval_context
from tools import delegate_tool_config

SESSION_KEY = "test-subagent-attribution"


@pytest.fixture
def gateway_session(monkeypatch):
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    for k in ("HERMES_CRON_SESSION", "HERMES_YOLO_MODE", "HERMES_INTERACTIVE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", SESSION_KEY)
    # ``--yolo`` is frozen at import from HERMES_YOLO_MODE; a host shell running yolo must not
    # auto-approve the gate under test.
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "manual", "timeout": 60})
    yield
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()


def _start_gate(*, origin: str = "", on_notify=None):
    """Run the real command gate on a worker thread bound to *origin*; return (thread, holder)."""
    holder = {}
    if on_notify is not None:
        mod.register_gateway_notify(SESSION_KEY, on_notify)

    def worker():
        with approval_context.approval_origin(origin):
            holder["result"] = mod.check_all_command_guards("rm -rf .git", "local")

    thread = threading.Thread(target=worker)
    thread.start()
    return thread, holder


def test_subagent_card_names_the_child(gateway_session):
    """The queued card carries the subagent label in the description and a structured origin."""
    captured = []
    notified = threading.Event()

    def notify(data):
        captured.append(dict(data))
        notified.set()

    thread, holder = _start_gate(origin="subagent: refactor the auth module", on_notify=notify)
    assert notified.wait(timeout=10), "approval was never enqueued"
    data = captured[0]
    assert data["description"].startswith("[subagent: refactor the auth module]")
    assert data["origin"] == "subagent: refactor the auth module"
    mod.resolve_gateway_approval(SESSION_KEY, "once")
    thread.join(timeout=10)
    assert holder["result"]["approved"] is True


def test_parent_gate_stays_unlabelled(gateway_session):
    """A session's own approval (no origin bound) keeps the plain card — no prefix, no origin key."""
    captured = []
    notified = threading.Event()

    def notify(data):
        captured.append(dict(data))
        notified.set()

    thread, holder = _start_gate(on_notify=notify)
    assert notified.wait(timeout=10), "approval was never enqueued"
    data = captured[0]
    assert "[subagent" not in data["description"]
    assert "origin" not in data
    mod.resolve_gateway_approval(SESSION_KEY, "deny")
    thread.join(timeout=10)
    assert holder["result"]["approved"] is False


def test_policy_deny_never_prompts(gateway_session, monkeypatch):
    """``delegation.subagent_approval: deny`` settles a subagent gate without queueing a card."""
    monkeypatch.setattr(approval_context, "subagent_approval_policy", lambda: "deny")
    captured = []
    thread, holder = _start_gate(origin="subagent: batch job", on_notify=lambda d: captured.append(d))
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert captured == [], "policy deny must not reach the gateway notifier"
    assert not mod.has_blocking_approval(SESSION_KEY)
    result = holder["result"]
    assert result["approved"] is False
    assert result.get("outcome") == "denied"
    assert "delegation policy" in result["message"]


def test_policy_approve_never_prompts(gateway_session, monkeypatch):
    """``delegation.subagent_approval: approve`` grants a subagent gate without queueing a card."""
    monkeypatch.setattr(approval_context, "subagent_approval_policy", lambda: "approve")
    captured = []
    thread, holder = _start_gate(origin="subagent: trusted batch", on_notify=lambda d: captured.append(d))
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert captured == [], "policy approve must not reach the gateway notifier"
    assert holder["result"]["approved"] is True


def test_policy_reader_mapping(monkeypatch):
    """The reader maps values (case/space tolerant) and inherits on unknown input."""
    import hermes_cli.config as conf

    def reader_for(value):
        monkeypatch.setattr(conf, "load_config_readonly",
                            lambda: {"delegation": {"subagent_approval": value}})
        return approval_context.subagent_approval_policy()

    assert reader_for("") == ""
    assert reader_for("inherit") == ""
    assert reader_for(" Deny ") == "deny"
    assert reader_for("APPROVE") == "approve"
    assert reader_for("yes") == ""  # unknown → inherit, never a silent approve


def test_callback_follows_policy_then_legacy_flag(monkeypatch):
    """The CLI worker callback obeys the policy first, the legacy flag only when policy is unset."""
    import tools.approval_context as ctx

    monkeypatch.setattr(ctx, "subagent_approval_policy", lambda: "approve")
    assert delegate_tool_config._get_subagent_approval_callback()("rm -rf x", "d") == "once"

    monkeypatch.setattr(ctx, "subagent_approval_policy", lambda: "deny")
    assert delegate_tool_config._get_subagent_approval_callback()("rm -rf x", "d") == "deny"

    monkeypatch.setattr(ctx, "subagent_approval_policy", lambda: "")
    monkeypatch.setattr(delegate_tool_config, "_cfg", lambda: {"subagent_auto_approve": True})
    assert delegate_tool_config._get_subagent_approval_callback()("rm -rf x", "d") == "once"

    monkeypatch.setattr(delegate_tool_config, "_cfg", dict)
    assert delegate_tool_config._get_subagent_approval_callback()("rm -rf x", "d") == "deny"


def test_origin_label_helper():
    from tools.delegate_tool_child_run import _subagent_origin_label

    assert _subagent_origin_label("fix the bug") == "subagent: fix the bug"
    assert _subagent_origin_label("a\n  b") == "subagent: a b"
    long = _subagent_origin_label("x" * 80)
    assert long.startswith("subagent: ") and long.endswith("…") and len(long) <= len("subagent: ") + 48
    assert _subagent_origin_label("") == "subagent"
