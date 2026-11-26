"""Per-input tool safety flags (B13, Claude Code Tool.ts parity).

Static ``is_read_only``/``is_concurrency_safe`` on HermesTool describe the worst
case; CC lets a tool answer PER INPUT (a search is read-only, a delete is not).
Pins: per-input callables override static attrs in ``classify_tool``, the parallel
admission path consults them when it has the call args, a raising callable falls
back to the STATIC answer (fail-safe, never crash tool dispatch), and tools without
callables behave exactly as before.
"""

import pytest

from tools import tool_base
from tools.concurrency import classify_tool
from tools.tool_base import HermesTool


class _PerInputTool(HermesTool):
    name = "per_input_demo"
    is_read_only = False        # worst case: a delete-like call
    is_concurrency_safe = False

    @staticmethod
    def is_read_only_for(args):
        return str(args.get("mode", "")) == "search"

    @staticmethod
    def is_concurrency_safe_for(args):
        return str(args.get("mode", "")) == "search"


class _RaisingTool(HermesTool):
    name = "raising_demo"
    is_read_only = True
    is_concurrency_safe = True

    @staticmethod
    def is_read_only_for(args):
        raise RuntimeError("boom")

    @staticmethod
    def is_concurrency_safe_for(args):
        raise RuntimeError("boom")


@pytest.fixture
def registered(monkeypatch):
    registry = {}
    monkeypatch.setattr(tool_base, "_tool_instances", registry)
    yield registry


class TestClassifyPerInput:

    def test_per_input_callable_overrides_static(self, registered):
        registered["per_input_demo"] = _PerInputTool()
        assert classify_tool("per_input_demo", {"mode": "search"}) == (True, True)
        assert classify_tool("per_input_demo", {"mode": "delete"}) == (False, False)

    def test_no_args_keeps_static_answer(self, registered):
        registered["per_input_demo"] = _PerInputTool()
        assert classify_tool("per_input_demo") == (False, False)

    def test_raising_callable_falls_back_to_static(self, registered):
        registered["raising_demo"] = _RaisingTool()
        assert classify_tool("raising_demo", {"x": 1}) == (True, True)


class TestAdmissionUsesPerInput:

    def test_admission_admits_readonly_input(self, registered):
        from agent.tool_dispatch_helpers import _is_tool_parallel_safe

        registered["per_input_demo"] = _PerInputTool()
        assert _is_tool_parallel_safe("per_input_demo", {"mode": "search"}) is True
        assert _is_tool_parallel_safe("per_input_demo", {"mode": "delete"}) is False

    def test_unknown_name_unchanged(self):
        from agent.tool_dispatch_helpers import _is_tool_parallel_safe

        assert _is_tool_parallel_safe("definitely_not_a_tool", {"mode": "search"}) is False
