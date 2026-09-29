#!/usr/bin/env python3
"""
Hermes Tool Base Class — Claude Code-Inspired

Provides a standardized base class for all Hermes tools with safe defaults,
inspired by Claude Code's `buildTool()` factory + `TOOL_DEFAULTS` pattern
(Tool.ts:697-792) and separable permission hooks (Tool.ts:484-516).

Every tool can optionally inherit from HermesTool to gain:
- is_read_only: bool         — True = safe for parallel execution
- is_concurrency_safe: bool  — True = can batch with other read-only tools
- should_defer: bool         — True = lazy-load schema (saves context)
- search_hint: str           — keywords for deferred tool discovery
- check_permissions()        — tool-specific permission rules
- validate_input()           — custom input validation before execution
- map_result()               — transform raw output to model-friendly text
- user_facing_name()         — human-readable name for UI/permission prompts

Usage:
    from tools.tool_base import HermesTool

    class MyTool(HermesTool):
        name = "my_tool"
        description = "Does something useful"
        is_read_only = True
        tags = {"file", "read"}

        def call(self, **kwargs) -> str:
            return json.dumps({"success": True})

        def map_result(self, result: str) -> str:
            data = json.loads(result)
            return f"Done: {data['success']}"
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


class HermesTool:
    """Base class for Hermes tools with Claude Code-inspired safe defaults."""

    # ── Identity ────────────────────────────────────────────────────────
    name: str = ""
    description: str = ""
    user_facing_label: str = ""  # Human-readable name for UI/permission prompts

    # ── Schema flags ────────────────────────────────────────────────────
    is_read_only: bool = False
    is_concurrency_safe: bool = False  # Can run in parallel with other safe tools
    should_defer: bool = False  # Lazy-load schema (save context tokens)
    search_hint: str = ""  # Keywords for tool_search when deferred

    # ── Permission (Claude Code: Tool.ts 484-516) ────────────────────────
    @staticmethod
    def check_permissions(args: Dict[str, Any]) -> Optional[str]:
        """Return None = allowed, or a string error message = blocked.

        Override in subclasses for tool-specific permission rules.
        Default: allow all (fail-closed = None means pass).
        """
        return None

    # ── Validation ──────────────────────────────────────────────────────
    @staticmethod
    def validate_input(args: Dict[str, Any]) -> Optional[str]:
        """Return None = valid, or a string error message.

        Override for custom validation beyond JSON schema.
        Default: pass through.
        """
        return None

    # ── Execution ───────────────────────────────────────────────────────
    def call(self, task_id: Optional[str] = None,
             session_id: Optional[str] = None, **kwargs) -> str:
        """Execute the tool. Must return a JSON string.

        Override in subclasses.
        """
        raise NotImplementedError(f"{self.name}.call() must be implemented")

    # ── Result mapping (Claude Code: mapToolResultToToolResultBlockParam) ─
    def map_result(self, raw_result: str, **context) -> str:
        """Transform raw tool output into model-friendly text.

        Override to hide internal fields, format for readability, or
        add behavioral nudges (like verification reminders).

        Default: pass through unchanged.
        """
        return raw_result

    # ── UI helpers ──────────────────────────────────────────────────────
    def render_progress(self, args: Dict[str, Any]) -> Optional[str]:
        """Return a progress description during tool execution, or None."""
        return None

    def render_error(self, error: str, args: Dict[str, Any]) -> str:
        """Format an error message for the model."""
        return json.dumps({
            "error": f"{self.name} failed",
            "detail": str(error)[:500],
            "tool": self.name,
        }, ensure_ascii=False)

    # ── Feature gating (Claude Code: feature() / isEnabled()) ────────────
    @staticmethod
    def is_enabled() -> bool:
        """Check if this tool is currently enabled.

        Override for feature-gated tools. Default: always enabled.
        """
        return True

    # ── Tags for auto-classification ────────────────────────────────────
    tags: Set[str] = set()


# ── Tool Registry Wrapper ──────────────────────────────────────────────

# Maps tool name -> HermesTool instance (populated by tools that adopt the base class)
_tool_instances: Dict[str, HermesTool] = {}


def register_hermes_tool(tool: HermesTool) -> None:
    """Register a HermesTool instance so it can be queried for capabilities."""
    if not tool.name:
        raise ValueError(f"Tool {tool!r} has no 'name' attribute")
    if tool.name in _tool_instances:
        logger.warning("Tool %s re-registered; overwriting previous instance", tool.name)
    _tool_instances[tool.name] = tool


def get_tool_instance(name: str) -> Optional[HermesTool]:
    """Look up a HermesTool by name."""
    return _tool_instances.get(name)


def get_read_only_tool_names() -> Set[str]:
    """Return set of tool names marked as read-only (concurrency-safe for parallel execution)."""
    return {name for name, tool in _tool_instances.items() if tool.is_read_only}


def get_deferred_tool_names() -> Set[str]:
    """Return set of tool names that should be lazy-loaded."""
    return {name for name, tool in _tool_instances.items() if tool.should_defer}


def get_concurrency_safe_tool_names() -> Set[str]:
    """Return set of tool names safe for parallel batch execution."""
    return {name for name, tool in _tool_instances.items() if tool.is_concurrency_safe}


def check_tool_permissions(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """Run tool-specific permission check. Returns None if allowed, error message if blocked."""
    tool = _tool_instances.get(tool_name)
    if tool is None:
        return None
    try:
        return tool.check_permissions(args)
    except Exception as exc:
        logger.warning("Permission check error for %s: %s", tool_name, exc)
        return f"Permission check failed: {exc}"


def validate_tool_input(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """Run custom input validation. Returns None if valid, error message if invalid."""
    tool = _tool_instances.get(tool_name)
    if tool is None:
        return None
    try:
        return tool.validate_input(args)
    except Exception as exc:
        logger.warning("Input validation error for %s: %s", tool_name, exc)
        return f"Validation failed: {exc}"


def map_tool_result(tool_name: str, raw_result: str, **context) -> str:
    """Transform raw tool output via the tool's map_result method."""
    tool = _tool_instances.get(tool_name)
    if tool is None:
        return raw_result
    try:
        return tool.map_result(raw_result, **context)
    except Exception as exc:
        logger.debug("map_result error for %s: %s", tool_name, exc)
        return raw_result