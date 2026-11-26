"""Gateway dispatch for /forget-approvals: admin-gated, clears both grant tiers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _event(text: str = "/forget-approvals") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="user-1",
            chat_id="chat-1",
            chat_type="dm",
        ),
    )


def _runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = SimpleNamespace(platforms={})
    runner.hooks = MagicMock(loaded_hooks=[])
    runner.hooks.emit = AsyncMock(return_value=[])
    runner._running_agents = {}
    runner._get_or_create_gateway_honcho = lambda _key: (None, None)
    runner._is_user_authorized = lambda _source: True
    runner.session_store = SimpleNamespace(get_or_create_session=lambda _source: None)
    return runner


@pytest.mark.asyncio
async def test_forget_approvals_clears_grants_and_reports_counts():
    runner = _runner()
    with patch("tools.approval.forget_all_approval_grants") as forget:
        forget.return_value = {"session": 2, "permanent": 3}
        output = await runner._handle_forget_approvals_command(_event())
    forget.assert_called_once()
    assert "2" in output and "3" in output


@pytest.mark.asyncio
async def test_forget_approvals_rejects_non_admin():
    runner = _runner()
    runner.config = SimpleNamespace(
        platforms={
            Platform.TELEGRAM: SimpleNamespace(
                extra={
                    "allow_admin_from": ["admin-1"],
                    "user_allowed_commands": ["forget-approvals"],
                }
            )
        }
    )
    with patch("tools.approval.forget_all_approval_grants") as forget:
        output = await runner._handle_forget_approvals_command(_event())
    assert "admin" in output.lower()
    forget.assert_not_called()
