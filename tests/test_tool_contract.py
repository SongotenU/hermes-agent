"""Tests for Phase 3 — per-tool safety metadata + registry-driven parallel dispatch."""

# Import tool modules to trigger registration
import tools.file_tools  # noqa: F401
import tools.delegate_tool  # noqa: F401

from tools.registry import registry
from agent.tool_dispatch_helpers import _is_tool_parallel_safe, _LEGACY_PARALLEL_SAFE_TOOLS


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
