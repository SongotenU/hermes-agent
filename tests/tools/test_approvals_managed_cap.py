"""Managed approval cap: ``approvals.max_mode`` in the managed scope clamps the mode and session /yolo.

Ported from T3 Code's permission modes capped by managed policy (managed-policy.md, repo pin
6577c6b61851). Hermes semantics under test:

- Ladder: ``off`` < ``smart`` < ``manual``. The effective mode is the STRICTER of the configured
  mode and the managed cap.
- ``/yolo`` bypasses every check, so it is more relaxed than ``off``: refused unless the cap is
  unset or ``off``.
- Absent managed scope = no cap = today's behaviour; an unknown cap value fails closed at ``manual``.
- The operator's frozen ``--yolo`` launch flag stays uncapped (root-equivalent authority).
"""
import threading

import pytest

from tools import approval as mod
from tools import approval_context
from tools import approval_yolo
from hermes_cli import managed_scope

SESSION_KEY = "test-managed-cap"


@pytest.fixture(autouse=True)
def _isolate_managed_scope(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    managed_scope.invalidate_managed_cache()
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    yield
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    managed_scope.invalidate_managed_cache()
    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_yolo.discard(SESSION_KEY)


def _managed_scope(monkeypatch, tmp_path, max_mode=None, *, raw: str | None = None):
    d = tmp_path / "managed"
    d.mkdir(exist_ok=True)
    if raw is not None:
        (d / "config.yaml").write_text(raw, encoding="utf-8")
    else:
        (d / "config.yaml").write_text(f"approvals:\n  max_mode: {max_mode}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(d))
    managed_scope.invalidate_managed_cache()


def _user_mode(monkeypatch, mode: str):
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": mode})


def test_no_managed_scope_keeps_todays_behaviour(monkeypatch):
    _user_mode(monkeypatch, "off")
    assert approval_context._get_approval_mode() == "off"
    assert approval_context.session_yolo_allowed() is True
    mod.enable_session_yolo(SESSION_KEY)
    assert mod.is_session_yolo_enabled(SESSION_KEY) is True


def test_cap_manual_clamps_off_mode(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, "manual")
    _user_mode(monkeypatch, "off")
    assert approval_context._get_approval_mode() == "manual"


def test_cap_is_a_floor_never_a_relaxation(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, "smart")
    _user_mode(monkeypatch, "off")
    assert approval_context._get_approval_mode() == "smart"
    _user_mode(monkeypatch, "manual")
    assert approval_context._get_approval_mode() == "manual"


def test_unknown_cap_value_fails_closed(monkeypatch, tmp_path, caplog):
    _managed_scope(monkeypatch, tmp_path, raw="approvals:\n  max_mode: yolo\n")
    _user_mode(monkeypatch, "off")
    assert approval_context._get_approval_mode() == "manual"
    assert any("max_mode" in rec.message for rec in caplog.records)


def test_cap_off_explicitly_allows_everything(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, "off")
    _user_mode(monkeypatch, "off")
    assert approval_context._get_approval_mode() == "off"
    assert approval_context.session_yolo_allowed() is True


def test_malformed_managed_file_is_no_cap(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, raw="approvals: [unclosed\n")
    _user_mode(monkeypatch, "off")
    assert approval_context._get_approval_mode() == "off"
    assert approval_context.session_yolo_allowed() is True


def test_yolo_refused_under_cap_and_persisted_off(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, "manual")
    persisted = []
    result = approval_yolo.toggle_session_yolo(SESSION_KEY, True, persist=persisted.append)
    assert result is False
    assert persisted == [False]
    assert mod.is_session_yolo_enabled(SESSION_KEY) is False


def test_yolo_toggle_off_still_works_under_cap(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, "manual")
    assert approval_yolo.toggle_session_yolo(SESSION_KEY, False) is False
    assert mod.is_session_yolo_enabled(SESSION_KEY) is False


def test_cap_appearing_after_arm_makes_yolo_inert(monkeypatch, tmp_path):
    mod.enable_session_yolo(SESSION_KEY)
    assert mod.is_session_yolo_enabled(SESSION_KEY) is True
    _managed_scope(monkeypatch, tmp_path, "manual")
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", False)
    import gateway.session_finite as finite
    monkeypatch.setattr(finite, "unattended_turn", lambda: False)
    assert mod._yolo_active() is False


def test_operator_yolo_flag_is_not_capped(monkeypatch, tmp_path):
    _managed_scope(monkeypatch, tmp_path, "manual")
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", True)
    assert mod._yolo_active() is True


def test_cap_forces_human_gate_end_to_end(monkeypatch, tmp_path):
    """With mode off but the cap at manual, a dangerous command queues a card again."""
    _managed_scope(monkeypatch, tmp_path, "manual")
    _user_mode(monkeypatch, "off")
    for k in ("HERMES_CRON_SESSION", "HERMES_YOLO_MODE", "HERMES_INTERACTIVE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_KEY", SESSION_KEY)
    monkeypatch.setattr(mod, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 60)

    notified = threading.Event()
    captured = []

    def notify(data):
        captured.append(dict(data))
        notified.set()

    mod.register_gateway_notify(SESSION_KEY, notify)
    holder = {}

    def worker():
        holder["result"] = mod.check_all_command_guards("rm -rf .git", "local")

    thread = threading.Thread(target=worker)
    thread.start()
    assert notified.wait(timeout=10), "capped mode 'off' must still queue a human approval"
    mod.resolve_gateway_approval(SESSION_KEY, "deny")
    thread.join(timeout=10)
    assert holder["result"]["approved"] is False
