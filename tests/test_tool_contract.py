"""Tests for Phase 3 — per-tool safety metadata + registry-driven parallel dispatch."""

import json
from types import SimpleNamespace
from unittest.mock import patch

# Import tool modules to trigger registration
import tools.file_tools  # noqa: F401
import tools.delegate_tool  # noqa: F401

from tools.registry import registry
from agent.tool_dispatch_helpers import (
    _batch_admission,
    _is_tool_parallel_safe,
    _PARALLEL_SAFE_TOOLS,
)


def _tool_call(name, arguments=None):
    return SimpleNamespace(
        id="call_contract_test",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})),
    )


def test_batch_admission_admits_legacy_frozenset_member():
    """Control: an unscoped frozenset member with no registry metadata is admitted."""
    assert "ha_get_state" in _PARALLEL_SAFE_TOOLS
    admitted = _batch_admission(_tool_call("ha_get_state"), None)
    assert admitted is not None
    assert admitted[0] == "ha_get_state"
    assert admitted[2] is False


def test_batch_admission_registry_false_overrides_frozenset():
    """R6.2/R6.4 red-on-base guard: _batch_admission must consult registry metadata
    through _is_tool_parallel_safe — an explicit is_concurrency_safe=False wins
    over the legacy frozenset and turns the call into a sequential barrier."""
    assert "ha_get_state" in _PARALLEL_SAFE_TOOLS  # legacy would admit it
    forced = {"source": "registry", "is_read_only": None, "is_concurrency_safe": False}
    with patch.object(registry, "get_tool_safety", return_value=forced):
        assert _batch_admission(_tool_call("ha_get_state"), None) is None


def test_registry_safety_explicit_read_file():
    entry = registry.get_entry("read_file")
    assert entry is not None
    assert entry.is_read_only is True
    assert entry.is_concurrency_safe is True


def test_get_tool_safety_explicit():
    s = registry.get_tool_safety("read_file")
    assert s["source"] == "registry"
    assert s["is_read_only"] is True
    assert s["is_concurrency_safe"] is True


def test_get_tool_safety_heuristic_unknown():
    s = registry.get_tool_safety("nonexistent_fake_tool_12345")
    assert s["source"] == "heuristic"
    assert s["is_read_only"] is None


def test_get_tool_safety_write_file_destructive():
    s = registry.get_tool_safety("write_file")
    assert s["source"] == "registry"
    assert s["is_read_only"] is False
    assert s["is_destructive"] is True
    assert s["is_concurrency_safe"] is False


def test_parallel_safe_uses_registry_first():
    assert _is_tool_parallel_safe("read_file") is True
    assert _is_tool_parallel_safe("write_file") is False


def test_legacy_frozenset_backward_compat():
    assert _is_tool_parallel_safe("ha_get_state") is True


def test_explicit_false_overrides_legacy():
    assert _is_tool_parallel_safe("write_file") is False
    assert _is_tool_parallel_safe("delegate_task") is False


def test_delegate_task_not_parallel():
    s = registry.get_tool_safety("delegate_task")
    assert s["source"] == "registry"
    assert s["is_concurrency_safe"] is False
