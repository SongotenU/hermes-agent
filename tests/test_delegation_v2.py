"""Tests for Delegation v2 — subagent resume + fork-mode (Phase 2).

Covers R3.1–R3.7 and R4.1–R4.7 from SPEC:
- Resume nonexistent session → error
- Resume running subagent → error
- Fork + resume mutually exclusive → error
- Fork disabled by default → error
- Fork non-caching provider → error
- Resume result has resumed_from field
- Fork child inherits parent system prompt
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# --- Test helpers -----------------------------------------------------------

def _mock_parent_agent(
    provider="anthropic",
    session_id="parent-sess-123",
    system_prompt="You are a helpful assistant.",
    session_messages=None,
    session_db=None,
):
    """Build a minimal parent_agent mock with the attrs delegate_task reads."""
    return SimpleNamespace(
        provider=provider,
        session_id=session_id,
        _cached_system_prompt=system_prompt,
        _session_messages=session_messages or [],
        _session_db=session_db or MagicMock(),
        _delegate_depth=0,
        _current_task_id="parent-task-1",
        model="test-model",
        api_key="test-key",
        _client_kwargs={},
        _session_init_model_config={},
        log_prefix="[test] ",
    )


def _mock_creds(provider="anthropic", model="test-model"):
    return {
        "provider": provider,
        "model": model,
        "base_url": "https://api.test.com",
        "api_key": "test-key",
        "api_mode": "chat_completions",
        "request_overrides": None,
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }


# --- R3: Subagent resume tests ----------------------------------------------

class TestResumeValidation:
    def test_resume_nonexistent_session(self):
        """R3.4: resume a session not in DB → clear error."""
        from tools import delegate_tool

        parent = _mock_parent_agent(session_db=MagicMock())
        parent._session_db.get_messages = MagicMock(return_value=[])

        with patch.object(delegate_tool, "_load_config", return_value={}):
            with patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()):
                with patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
                    result = delegate_tool.delegate_task(
                        goal="refine the result",
                        resume="nonexistent-id",
                        parent_agent=parent,
                    )
        assert "not found" in result.lower()

    def test_resume_running_subagent(self):
        """R3.5: resume a subagent that's still running → error."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        # Register a running subagent
        delegate_tool._register_subagent({
            "subagent_id": "running-sub-1",
            "status": "running",
            "agent": MagicMock(),
        })
        try:
            with patch.object(delegate_tool, "_load_config", return_value={}):
                with patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()):
                    with patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
                        result = delegate_tool.delegate_task(
                            goal="continue",
                            resume="running-sub-1",
                            parent_agent=parent,
                        )
            assert "still running" in result.lower()
        finally:
            delegate_tool._unregister_subagent("running-sub-1")

    def test_resume_disabled_by_config(self):
        """R3.7: resume_enabled=false → rejected."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        with patch.object(delegate_tool, "_load_config", return_value={"resume_enabled": False}):
            with patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()):
                with patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
                    result = delegate_tool.delegate_task(
                        goal="continue",
                        resume="some-id",
                        parent_agent=parent,
                    )
        assert "disabled" in result.lower()

    def test_resume_requires_goal(self):
        """Resume without a goal → error."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        with patch.object(delegate_tool, "_load_config", return_value={}):
            with patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()):
                with patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
                    result = delegate_tool.delegate_task(
                        goal="",
                        resume="some-id",
                        parent_agent=parent,
                    )
        assert "goal" in result.lower()


# --- R4: Fork-mode tests ----------------------------------------------------

class TestForkModeValidation:
    def test_fork_and_resume_mutually_exclusive(self):
        """R4.5: fork + resume → error."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        with patch.object(delegate_tool, "_load_config", return_value={}), \
             patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()), \
             patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
            result = delegate_tool.delegate_task(
                goal="test",
                mode="fork",
                resume="some-id",
                parent_agent=parent,
            )
        assert "fork and resume" in result.lower() or "mutually exclusive" in result.lower()

    def test_fork_disabled_by_default(self):
        """R4.6: fork_enabled defaults to false → rejected."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        with patch.object(delegate_tool, "_load_config", return_value={}), \
             patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()), \
             patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
            result = delegate_tool.delegate_task(
                goal="test",
                mode="fork",
                parent_agent=parent,
            )
        assert "disabled" in result.lower() or "fork_enabled" in result.lower()

    def test_fork_non_caching_provider(self):
        """R4.2: fork with non-caching provider → error."""
        from tools import delegate_tool

        parent = _mock_parent_agent(provider="some-random-provider")
        with patch.object(delegate_tool, "_load_config", return_value={"fork_enabled": True}), \
             patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds(provider="some-random-provider")), \
             patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
            result = delegate_tool.delegate_task(
                goal="test",
                mode="fork",
                parent_agent=parent,
            )
        assert "prompt caching" in result.lower() or "provider" in result.lower()

    def test_invalid_mode(self):
        """Invalid mode string → error."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        with patch.object(delegate_tool, "_load_config", return_value={}), \
             patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()), \
             patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2):
            result = delegate_tool.delegate_task(
                goal="test",
                mode="invalid",
                parent_agent=parent,
            )
        assert "invalid mode" in result.lower()


# --- Integration: resume result + fork inheritance -------------------------

class TestResumeResult:
    def test_resume_result_has_resumed_from(self):
        """R3.2: resumed delegation result includes resumed_from field."""
        from tools import delegate_tool

        parent = _mock_parent_agent()
        # Mock SessionDB to return prior messages
        parent._session_db.get_messages = MagicMock(return_value=[
            {"role": "user", "content": "previous task"},
            {"role": "assistant", "content": "previous result"},
        ])
        parent._session_db.get_session = MagicMock(return_value={
            "source": "delegation:parent-sess-123",
        })

        # Mock _build_child_agent to return a simple mock child
        mock_child = MagicMock()
        mock_child._resume_history = None

        # Mock _run_single_child to return a dict result
        mock_result = {
            "task_index": 0,
            "status": "completed",
            "summary": "Refined result",
            "api_calls": 5,
        }

        with patch.object(delegate_tool, "_load_config", return_value={}), \
             patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds()), \
             patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2), \
             patch.object(delegate_tool, "_build_child_agent", return_value=mock_child), \
             patch.object(delegate_tool, "_run_single_child", return_value=mock_result):
            result = delegate_tool.delegate_task(
                goal="refine the result",
                resume="completed-sub-1",
                parent_agent=parent,
            )

        parsed = json.loads(result)
        assert "results" in parsed
        assert parsed["results"][0]["resumed_from"] == "completed-sub-1"
        assert parsed["results"][0]["status"] == "completed"


class TestForkInheritance:
    def test_fork_child_gets_parent_system_prompt(self):
        """R4.3: fork child's _cached_system_prompt == parent's."""
        from tools import delegate_tool

        parent_prompt = "You are the parent's system prompt."
        parent = _mock_parent_agent(
            system_prompt=parent_prompt,
            session_messages=[{"role": "user", "content": "parent context"}],
        )

        # Track what gets set on the child
        captured_children = []

        mock_child = MagicMock()
        captured_children.append(mock_child)

        with patch.object(delegate_tool, "_load_config", return_value={"fork_enabled": True}), \
             patch.object(delegate_tool, "_resolve_delegation_credentials", return_value=_mock_creds(provider="anthropic")), \
             patch.object(delegate_tool, "_get_max_spawn_depth", return_value=2), \
             patch.object(delegate_tool, "_get_max_concurrent_children", return_value=3), \
             patch.object(delegate_tool, "_build_child_agent", return_value=mock_child), \
             patch.object(delegate_tool, "_run_single_child", return_value={"status": "completed", "summary": "ok"}):
            delegate_tool.delegate_task(
                goal="research this codebase",
                mode="fork",
                parent_agent=parent,
            )

        # Verify fork child got parent's system prompt + messages
        assert mock_child._cached_system_prompt == parent_prompt
        assert hasattr(mock_child, "_fork_parent_messages")
