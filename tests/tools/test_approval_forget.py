"""`/forget-approvals` core (oh-my-pi clearApprovalGrants parity).

Revokes BOTH standing-approval tiers: this session's grants and every persisted
'always' pattern (``command_allowlist`` in config.yaml). The allowlist is an
operator file and ``save_permanent_allowlist`` deliberately only ADDS; an explicit
forget command is the one sanctioned path that removes entries.
"""

import pytest

import hermes_yaml as yaml
from hermes_cli.config import load_config_readonly, save_config
from tools import approval


@pytest.fixture
def seeded_allowlist():
    cfg = load_config_readonly()
    cfg = dict(cfg)
    cfg["command_allowlist"] = ["rm -rf *", "dd if=*"]
    save_config(cfg)
    approval.load_permanent_allowlist()
    yield
    approval.forget_all_approval_grants()


class TestForgetAllGrants:

    def test_clears_session_and_permanent_tiers(self, seeded_allowlist):
        approval.approve_session("sforget", "cmd:rm -rf *")
        approval.approve_permanent("cmd:dd if=*")
        assert approval.is_approved("sforget", "cmd:rm -rf *") is True
        assert approval.is_approved("other-session", "cmd:dd if=*") is True

        result = approval.forget_all_approval_grants("sforget")

        assert result["session"] == 1
        assert result["permanent"] >= 2
        assert approval.is_approved("sforget", "cmd:rm -rf *") is False
        assert approval.is_approved("other-session", "cmd:dd if=*") is False

    def test_removes_persisted_allowlist_from_config(self, seeded_allowlist):
        assert load_config_readonly().get("command_allowlist") == ["rm -rf *", "dd if=*"]
        approval.forget_all_approval_grants()
        assert load_config_readonly().get("command_allowlist") == []

    def test_idempotent_second_call_is_empty(self, seeded_allowlist):
        approval.approve_session("sforget", "cmd:x")
        approval.forget_all_approval_grants("sforget")
        assert approval.forget_all_approval_grants("sforget") == {"session": 0, "permanent": 0}

    def test_does_not_affect_yolo(self, seeded_allowlist):
        approval.enable_session_yolo("syolo")
        approval.forget_all_approval_grants("syolo")
        assert approval.is_session_yolo_enabled("syolo") is True
