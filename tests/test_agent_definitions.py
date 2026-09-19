"""Tests for Phase 4 — agent definitions + delegate_task integration."""

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


class TestParseAgentFile:
    def test_parse_frontmatter_and_body(self, tmp_path):
        from agent.agent_definition import _parse_agent_file

        md = tmp_path / "test_role.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: test_role
            description: A test role
            toolsets:
              - web
              - search
            model: claude-test
            ---
            You are a test role agent. Be precise.
        """))

        result = _parse_agent_file(md)
        assert result is not None
        assert result.name == "test_role"
        assert result.description == "A test role"
        assert result.toolsets == ["web", "search"]
        assert result.model == "claude-test"
        assert "test role" in result.body

    def test_parse_no_frontmatter(self, tmp_path):
        from agent.agent_definition import _parse_agent_file

        md = tmp_path / "simple.md"
        md.write_text("You are a simple agent.")

        result = _parse_agent_file(md)
        assert result is not None
        assert result.name == "simple"
        assert "simple agent" in result.body


class TestLoaderPrecedence:
    def test_load_nonexistent_returns_none(self):
        from agent.agent_definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader()
        assert loader.load("nonexistent_role_xyz") is None

    def test_builtin_definitions_load(self):
        from agent.agent_definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader()
        all_defs = loader.load_all()
        assert "default" in all_defs or "worker" in all_defs or "researcher" in all_defs


class TestApplyAgentDefinition:
    def test_apply_toolsets_restriction(self):
        from tools.delegate_tool import _apply_agent_definition
        from agent.agent_definition import AgentDefinition

        child = SimpleNamespace(
            enabled_toolsets=["web", "search", "terminal", "file", "delegation"],
            model="parent-model",
        )
        parent = SimpleNamespace(
            enabled_toolsets=["web", "search", "terminal", "file", "delegation"],
        )
        mock_def = AgentDefinition(
            name="researcher",
            toolsets=["web", "search", "file"],
            model=None,
            body="You are a researcher.",
        )

        with patch("agent.agent_definition.get_loader") as mock_get_loader:
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_get_loader.return_value = mock_loader
            _apply_agent_definition(child, "researcher", parent)

        assert set(child.enabled_toolsets) == {"web", "search", "file"}
        assert hasattr(child, "_agent_definition_body")
        assert "researcher" in child._agent_definition_body

    def test_apply_model_override(self):
        from tools.delegate_tool import _apply_agent_definition
        from agent.agent_definition import AgentDefinition

        child = SimpleNamespace(enabled_toolsets=["web"], model="parent-model")
        parent = SimpleNamespace(enabled_toolsets=["web"])
        mock_def = AgentDefinition(
            name="specialist", toolsets=None, model="specialist-model", body="",
        )

        with patch("agent.agent_definition.get_loader") as mock_get_loader:
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_get_loader.return_value = mock_loader
            _apply_agent_definition(child, "specialist", parent)

        assert child.model == "specialist-model"

    def test_no_definition_fallback(self):
        from tools.delegate_tool import _apply_agent_definition

        child = SimpleNamespace(enabled_toolsets=["web"], model="parent-model")
        parent = SimpleNamespace(enabled_toolsets=["web"])

        with patch("agent.agent_definition.get_loader") as mock_get_loader:
            mock_loader = MagicMock()
            mock_loader.load.return_value = None
            mock_get_loader.return_value = mock_loader
            _apply_agent_definition(child, "nonexistent", parent)

        assert child.enabled_toolsets == ["web"]
        assert child.model == "parent-model"
        assert not hasattr(child, "_agent_definition_body")

    def test_body_stashed_on_child(self):
        from tools.delegate_tool import _apply_agent_definition
        from agent.agent_definition import AgentDefinition

        child = SimpleNamespace(enabled_toolsets=["web"], model="m")
        parent = SimpleNamespace(enabled_toolsets=["web"])
        mock_def = AgentDefinition(
            name="worker", toolsets=None, model=None,
            body="You are a hands-on engineer.",
        )

        with patch("agent.agent_definition.get_loader") as mock_get_loader:
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_get_loader.return_value = mock_loader
            _apply_agent_definition(child, "worker", parent)

        assert child._agent_definition_body == "You are a hands-on engineer."


class TestDefinitionWiringAtSpawn:
    """Invariant: _build_children must apply a definition when the task role
    names one (the call site was once silently dropped from the spawn path)."""

    @staticmethod
    def _run_build(task_list):
        from tools import delegate_tool

        built = []

        def fake_build(**kwargs):
            child = SimpleNamespace(
                enabled_toolsets=["web"], model="parent-model",
                tool_progress_callback=None,
            )
            built.append(child)
            return child

        with patch.object(delegate_tool, "_build_child_preserving_parent_tools", fake_build):
            children, err = delegate_tool._build_children(
                task_list, [None] * len(task_list),
                {"provider": None, "base_url": None, "api_key": None, "api_mode": None, "model": None},
                top_role="leaf", max_iterations=10,
                parent_agent=SimpleNamespace(), routing_cfg={},
                live_deleg_id=None, live_writers=[],
            )
        assert err is None
        return children

    def test_named_role_applies_definition(self):
        from agent.agent_definition import AgentDefinition

        mock_def = AgentDefinition(
            name="researcher", toolsets=None, model=None,
            body="You are a researcher.",
        )
        with patch("agent.agent_definition.get_loader") as mock_get_loader:
            mock_loader = MagicMock()
            mock_loader.load.return_value = mock_def
            mock_get_loader.return_value = mock_loader
            children = self._run_build([{"goal": "g", "role": "researcher"}])

        child = children[0][2]
        assert child._agent_definition_body == "You are a researcher."

    def test_unknown_role_leaves_child_unchanged(self):
        with patch("agent.agent_definition.get_loader") as mock_get_loader:
            mock_loader = MagicMock()
            mock_loader.load.return_value = None
            mock_get_loader.return_value = mock_loader
            children = self._run_build([{"goal": "g"}])

        child = children[0][2]
        assert not hasattr(child, "_agent_definition_body")
        assert child.model == "parent-model"
