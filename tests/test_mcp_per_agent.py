"""Tests for Phase 5 — per-agent MCP server lifecycle (R9-R10)."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


class TestMcpConnect:
    def test_mcp_servers_connected(self):
        """R9.1: mcp_servers in definition → register_mcp_servers called."""
        from tools.delegate_tool import _apply_agent_definition
        from agent.agent_definition import AgentDefinition

        child = SimpleNamespace(enabled_toolsets=["web"], model="m")
        parent = SimpleNamespace(enabled_toolsets=["web"])
        mock_def = AgentDefinition(
            name="researcher",
            mcp_servers=["db_server", "search_server"],
            body="",
        )

        with patch("agent.agent_definition.get_loader") as mock_gl, \
             patch("tools.mcp_tool.register_mcp_servers") as mock_reg, \
             patch("tools.mcp_tool._load_mcp_config", return_value={
                 "db_server": {"command": "db"},
                 "search_server": {"command": "search"},
                 "global_server": {"command": "global"},
             }):
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_gl.return_value = mock_loader
            _apply_agent_definition(child, "researcher", parent)

        mock_reg.assert_called_once()
        called = mock_reg.call_args[0][0]
        assert "db_server" in called
        assert "search_server" in called
        assert "global_server" not in called
        assert hasattr(child, "_agent_mcp_servers")

    def test_nonexistent_server_skipped(self):
        """R9.3: server not in config → skipped."""
        from tools.delegate_tool import _apply_agent_definition
        from agent.agent_definition import AgentDefinition

        child = SimpleNamespace(enabled_toolsets=["web"], model="m")
        parent = SimpleNamespace(enabled_toolsets=["web"])
        mock_def = AgentDefinition(
            name="researcher", mcp_servers=["real", "fake"], body="",
        )

        with patch("agent.agent_definition.get_loader") as mock_gl, \
             patch("tools.mcp_tool.register_mcp_servers") as mock_reg, \
             patch("tools.mcp_tool._load_mcp_config", return_value={
                 "real": {"command": "r"},
             }):
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_gl.return_value = mock_loader
            _apply_agent_definition(child, "researcher", parent)

        called = mock_reg.call_args[0][0]
        assert "real" in called
        assert "fake" not in called
        assert child._agent_mcp_servers == ["real"]

    def test_no_mcp_definition(self):
        """Agent def without mcp_servers → no MCP change."""
        from tools.delegate_tool import _apply_agent_definition
        from agent.agent_definition import AgentDefinition

        child = SimpleNamespace(enabled_toolsets=["web"], model="m")
        parent = SimpleNamespace(enabled_toolsets=["web"])
        mock_def = AgentDefinition(name="worker", mcp_servers=None, body="")

        with patch("agent.agent_definition.get_loader") as mock_gl, \
             patch("tools.mcp_tool.register_mcp_servers") as mock_reg:
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_gl.return_value = mock_loader
            _apply_agent_definition(child, "worker", parent)

        mock_reg.assert_not_called()
        assert not hasattr(child, "_agent_mcp_servers")


class TestMcpCleanup:
    def test_cleanup_deregisters_child_servers(self):
        """R10.2: after child finishes, its MCP servers cleaned up."""
        from tools.delegate_tool import _run_single_child

        mock_child = MagicMock()
        mock_child._agent_mcp_servers = ["db_server"]
        mock_child.tool_progress_callback = None
        mock_child.session_prompt_tokens = 100
        mock_child.session_completion_tokens = 50
        mock_child.model = "test"
        mock_child.run_conversation.return_value = MagicMock(
            content="done", tool_calls=None, tool_results=[],
        )
        mock_mcp_task = MagicMock()
        mock_mcp_task._registered_tool_names = ["mcp_db_query"]

        from threading import Lock
        mcp_lock = Lock()

        with patch("tools.mcp_tool._servers", {"db_server": mock_mcp_task}), \
             patch("tools.mcp_tool._lock", mcp_lock), \
             patch("tools.registry.registry") as mock_reg:
            _run_single_child(0, "test", mock_child, None)

        mock_reg.deregister.assert_any_call("mcp_db_query")
