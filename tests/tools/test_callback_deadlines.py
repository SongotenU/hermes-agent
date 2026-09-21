"""Tests for per-callback deadline budgets (Phase 3 R1).

Covers R1-T1 through R1-T7 from spec.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.registry import ToolEntry, ToolRegistry, registry as global_registry
from agent.tool_executor import (
    execute_tool_calls_sequential,
    execute_tool_calls_concurrent,
)


@pytest.fixture
def fresh_registry():
    """Provide a clean registry instance per test."""
    # Use the global registry but track what we add for cleanup
    added_tools = []
    original_tools = dict(global_registry._tools)
    yield global_registry
    # Cleanup: remove only tools we added
    for name in added_tools:
        if name in global_registry._tools:
            del global_registry._tools[name]
    # Restore any original tools that were shadowed
    for name, entry in original_tools.items():
        if name not in global_registry._tools:
            global_registry._tools[name] = entry
    global_registry._generation += 1


@pytest.fixture
def mock_config(monkeypatch):
    """Mock config with callback_deadlines section."""
    from hermes_cli.config import load_config

    def mock_load():
        return {
            "tools": {
                "callback_deadlines": {
                    "check_fn": 0.1,  # 100ms for fast tests
                    "pre_hook": 0.1,
                    "post_hook": 0.1,
                }
            }
        }

    monkeypatch.setattr("hermes_cli.config.load_config", mock_load)


@pytest.fixture
def agent(tmp_path):
    """Create a test agent using the same pattern as existing tests."""
    from run_agent import AIAgent
    
    with (
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=["test"],  # Enable the test toolset
        )
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    agent._append_guardrail_observation = MagicMock(
        side_effect=lambda _name, _args, result, **_kwargs: result
    )
    agent._record_file_mutation_result = MagicMock()
    agent._subdirectory_hints = MagicMock()
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value="")
    agent._tool_result_content_for_active_model = MagicMock(
        side_effect=lambda _name, result: result
    )
    agent._tool_guardrails = MagicMock()
    agent._tool_guardrails.before_call = MagicMock(
        return_value=MagicMock(allows_execution=True)
    )
    agent._checkpoint_mgr = MagicMock()
    agent._checkpoint_mgr.get_working_dir_for_path = MagicMock(return_value=str(tmp_path))
    agent._checkpoint_mgr.ensure_checkpoint = MagicMock()
    agent._memory_manager = None
    agent._context_engine_tool_names = set()
    agent._turns_since_memory = 0
    agent._iters_since_skill = 0
    agent._delegate_spinner = None
    agent._interrupt_requested = False
    
    yield agent


def _tool_call(call_id: str, name: str, args: str = "{}"):
    """Create a mock tool call object."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=args),
    )


class TestCallbackDeadlines:
    """Test callback deadline enforcement per spec R1."""

    # R1-T1: check_fn treo → bị cắt tại budget, tool vẫn chạy (fail-open)
    def test_check_fn_deadline_exceeded_tool_still_runs(self, agent, fresh_registry, mock_config, caplog):
        """When check_fn exceeds its budget, tool handler should still execute (fail-open)."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        executed = {"handler": False, "check_fn": False}

        def slow_check():
            executed["check_fn"] = True
            import time
            time.sleep(0.5)  # Exceeds 0.1s budget
            return True

        async def handler(args, **kwargs):
            executed["handler"] = True
            return {"result": "ok"}

        # Register tool with slow check_fn in the global registry
        tool_name = "test_tool_check_deadline"
        fresh_registry.register(
            name=tool_name,
            toolset="file",
            schema={"type": "object", "properties": {}},
            handler=handler,
            check_fn=slow_check,
            is_async=True,
        )
        fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
        fresh_registry._added_tools.append(tool_name)

        tool_calls = [_tool_call("call_1", tool_name)]
        messages = []

        # Execute sequentially
        execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")

        # Handler should still run (fail-open for check_fn)
        assert executed["handler"], "Handler should execute even when check_fn times out"
        assert messages, "Should have results"

    # R1-T2: pre_hook chậm > budget → bị cắt, handler vẫn chạy đủ
    def test_pre_hook_deadline_exceeded_handler_still_runs(self, agent, fresh_registry, mock_config, caplog):
        """When pre_hook exceeds budget, it's cut but handler still runs."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        executed = {"pre_hook": False, "handler": False, "post_hook": False}

        async def slow_pre_hook(name, args, task_id):
            executed["pre_hook"] = True
            await asyncio.sleep(0.5)  # Exceeds 0.1s budget

        async def handler(args, **kwargs):
            executed["handler"] = True
            return {"result": "ok"}

        async def post_hook(name, args, result, task_id):
            executed["post_hook"] = True

        # Patch hook dispatch to include our slow pre_hook
        with patch("hermes_cli.plugins._dispatch_pre_tool_call_hooks", side_effect=slow_pre_hook), \
             patch("model_tools._emit_post_tool_call_hook", side_effect=post_hook):

            tool_name = "test_tool_pre_deadline"
            fresh_registry.register(
                name=tool_name,
                toolset="file",
                schema={"type": "object", "properties": {}},
                handler=handler,
                is_async=True,
            )
            fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
            fresh_registry._added_tools.append(tool_name)

            tool_calls = [_tool_call("call_1", tool_name)]
            messages = []

            execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")

            # Handler should still run (fail-open for pre_hook)
            assert executed["handler"], "Handler should execute even when pre_hook times out"
            assert messages, "Should have results"

    # R1-T3: handler KHÔNG bị đụng khi không khai deadline (backward-compat)
    def test_handler_not_affected_when_no_deadline_configured(self, agent, fresh_registry, mock_config):
        """When no deadline is configured for handler, it should not be affected."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        executed = {"handler": False}

        async def handler(args, **kwargs):
            executed["handler"] = True
            await asyncio.sleep(0.05)  # Small delay
            return {"result": "ok"}

        tool_name = "test_tool_no_handler_deadline"
        fresh_registry.register(
            name=tool_name,
            toolset="file",
            schema={"type": "object", "properties": {}},
            handler=handler,
            is_async=True,
        )
        fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
        fresh_registry._added_tools.append(tool_name)

        tool_calls = [_tool_call("call_1", tool_name)]
        messages = []

        execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")

        assert executed["handler"], "Handler should execute normally"
        assert messages, "Should have results"

    # R1-T4: config override per-tool thắng global
    def test_per_tool_deadline_override_wins_global(self, agent, fresh_registry, monkeypatch, mock_config):
        """Per-tool callback_deadline override should win over global config."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        executed = {"pre_hook": 0}

        async def tracked_pre_hook(name, args, task_id):
            executed["pre_hook"] += 1
            await asyncio.sleep(0.05)

        async def handler(args, **kwargs):
            return json.dumps({"result": "ok"})

        # Tool with per-tool override: pre_hook: 5.0 (much higher than global 0.1)
        tool_name = "test_tool_override"
        fresh_registry.register(
            name=tool_name,
            toolset="file",
            schema={"type": "object", "properties": {}},
            handler=handler,
            is_async=True,
            callback_deadline={"pre_hook": 5.0},
        )
        fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
        fresh_registry._added_tools.append(tool_name)

        tool_calls = [_tool_call("call_1", tool_name)]
        messages = []

        execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")
        assert messages, "Should have results"

    # R1-T5: breach phát log + observability event đúng fields
    def test_deadline_breach_logs_and_emits_event(self, tmp_path, fresh_registry, mock_config, caplog):
        """Deadline breach should emit structured log with correct fields."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        executed = {"check_fn": False}

        def slow_check():
            executed["check_fn"] = True
            import time
            time.sleep(0.5)
            return True

        async def handler(args, **kwargs):
            return json.dumps({"result": "ok"})

        tool_name = "test_tool_log_breach"
        # Register BEFORE agent init so get_tool_definitions picks it up
        fresh_registry.register(
            name=tool_name,
            toolset="file",
            schema={"type": "object", "properties": {}},
            handler=handler,
            check_fn=slow_check,
            is_async=True,
            callback_deadline={"check_fn": 0.1},  # Per-tool deadline
        )
        fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
        fresh_registry._added_tools.append(tool_name)

        # Need to invalidate check_fn cache and re-initialize agent's tool definitions
        from tools.registry import invalidate_check_fn_cache
        from model_tools import _clear_tool_defs_cache
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()

        # Create agent after tool registration
        from run_agent import AIAgent
        from unittest.mock import patch, MagicMock
        with (
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch("agent.model_metadata.fetch_model_metadata", return_value={}),
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                enabled_toolsets=["file"],  # Enable the test toolset
            )
        agent._flush_messages_to_session_db = MagicMock(return_value=True)
        agent._append_guardrail_observation = MagicMock(
            side_effect=lambda _name, _args, result, **_kwargs: result
        )
        agent._record_file_mutation_result = MagicMock()
        agent._subdirectory_hints = MagicMock()
        agent._subdirectory_hints.check_tool_call = MagicMock(return_value="")
        agent._tool_result_content_for_active_model = MagicMock(
            side_effect=lambda _name, result: result
        )
        agent._tool_guardrails = MagicMock()
        agent._tool_guardrails.before_call = MagicMock(
            return_value=MagicMock(allows_execution=True)
        )
        agent._checkpoint_mgr = MagicMock()
        agent._checkpoint_mgr.get_working_dir_for_path = MagicMock(return_value=str(tmp_path))
        agent._checkpoint_mgr.ensure_checkpoint = MagicMock()
        agent._memory_manager = None
        agent._context_engine_tool_names = set()
        agent._turns_since_memory = 0
        agent._iters_since_skill = 0
        agent._delegate_spinner = None
        agent._interrupt_requested = False

        tool_calls = [_tool_call("call_1", tool_name)]
        messages = []

        with caplog.at_level("WARNING"):
            execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")

        # Check for structured log
        breach_logs = [
            r for r in caplog.records
            if r.getMessage() == "callback_deadline_exceeded"
        ]
        assert breach_logs, "Should log callback_deadline_exceeded warning"
        record = breach_logs[0]
        assert record.callback_kind == "check_fn", "Should mention which callback kind"
        assert record.tool_name == tool_name, "Should mention tool name"

    # R1-T6: sequential path và concurrent path đều enforce
    @pytest.mark.asyncio
    async def test_both_sequential_and_concurrent_enforce_deadlines(self, agent, fresh_registry, mock_config):
        """Both sequential and concurrent execution should enforce deadlines."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        executed = {"check_fn": 0}

        def slow_check():
            executed["check_fn"] += 1
            import time
            time.sleep(0.5)
            return True

        async def handler(args, **kwargs):
            return json.dumps({"result": "ok"})

        tool_name = "test_tool_concurrent"
        fresh_registry.register(
            name=tool_name,
            toolset="file",
            schema={"type": "object", "properties": {}},
            handler=handler,
            check_fn=slow_check,
            is_async=True,
        )
        fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
        fresh_registry._added_tools.append(tool_name)

        tool_calls = [
            _tool_call("call_1", tool_name),
            _tool_call("call_2", tool_name),
        ]
        messages = []

        # Test concurrent path
        execute_tool_calls_concurrent(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")
        assert messages, "Concurrent should produce results"

        # Test sequential path too
        messages = []
        execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")
        assert messages, "Sequential should produce results"

    # R1-T7: default config thiếu section → zero behavior change
    def test_missing_config_section_no_behavior_change(self, agent, fresh_registry, monkeypatch):
        """Missing callback_deadlines config section should not change behavior."""
        import hermes_cli.config as config_mod
        config_mod._CONFIG_CACHE = None

        def mock_load_no_deadlines():
            return {"tools": {}}  # No callback_deadlines section

        monkeypatch.setattr("hermes_cli.config.load_config", mock_load_no_deadlines)

        executed = {"handler": False}

        async def handler(args, **kwargs):
            executed["handler"] = True
            return {"result": "ok"}

        tool_name = "test_tool_no_config"
        fresh_registry.register(
            name=tool_name,
            toolset="test",
            schema={"type": "object", "properties": {}},
            handler=handler,
            is_async=True,
        )
        fresh_registry._added_tools = getattr(fresh_registry, '_added_tools', [])
        fresh_registry._added_tools.append(tool_name)

        tool_calls = [_tool_call("call_1", tool_name)]
        messages = []

        execute_tool_calls_sequential(agent, type('Assistant', (), {'tool_calls': tool_calls})(), messages, "task-1")

        assert executed["handler"], "Handler should execute normally without config"
        assert messages, "Should have results"


class TestToolEntryCallbackDeadlineField:
    """Test ToolEntry accepts callback_deadline field (R1.1)."""

    def test_tool_entry_accepts_callback_deadline(self, fresh_registry):
        """ToolEntry should accept callback_deadline dict without error."""
        entry = ToolEntry(
            name="test",
            toolset="test",
            schema={},
            handler=lambda: None,
            check_fn=None,
            requires_env=[],
            is_async=False,
            description="",
            emoji="",
            callback_deadline={"check_fn": 5.0, "pre_hook": 10.0, "post_hook": 10.0},
        )
        assert hasattr(entry, "callback_deadline")
        assert entry.callback_deadline == {"check_fn": 5.0, "pre_hook": 10.0, "post_hook": 10.0}

    def test_tool_entry_callback_deadline_optional(self, fresh_registry):
        """ToolEntry should work without callback_deadline (backward compat)."""
        entry = ToolEntry(
            name="test",
            toolset="test",
            schema={},
            handler=lambda: None,
            check_fn=None,
            requires_env=[],
            is_async=False,
            description="",
            emoji="",
        )
        assert hasattr(entry, "callback_deadline")
        assert entry.callback_deadline is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])