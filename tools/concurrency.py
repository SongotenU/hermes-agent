#!/usr/bin/env python3
"""
Tool Concurrency Partition — Claude Code-Inspired

Implements smart partitioning of tool calls into parallel-safe and serial
batches, inspired by Claude Code's toolOrchestration.ts:84-116
(partitionToolCalls) and StreamingToolExecutor.ts.

Read-only tools are grouped into concurrent batches; mutating tools
run sequentially to avoid race conditions. This is the same pattern
Claude Code uses: read_file, grep, glob, web_search run in parallel;
write_file, patch, terminal run one at a time.

Integration point: called from run_agent.py before dispatching tool calls.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Default classification ──────────────────────────────────────────────
# These are conservative defaults. Tools that adopt HermesTool base class
# can override via is_read_only / is_concurrency_safe attributes.

# Read-only tools: safe for parallel execution (no side effects)
_READ_ONLY_DEFAULTS: Set[str] = {
    "read_file", "search_files", "web_search", "web_extract",
    "browser_snapshot", "browser_get_images", "browser_vision",
    "browser_console", "vision_analyze", "session_search",
    "skills_list", "skill_view", "ha_get_state", "ha_list_entities",
    "ha_list_services", "kanban_show", "kanban_list",
    "spotify_search", "spotify_playlists", "spotify_albums",
    "spotify_library", "spotify_devices",
    "tool_search", "tool_describe",
}

# Mutating tools: MUST run sequentially (file writes, shell commands, etc.)
_MUTATING_DEFAULTS: Set[str] = {
    "write_file", "patch", "terminal", "execute_code",
    "delegate_task", "browser_navigate", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_press", "browser_cdp", "browser_dialog",
    "computer_use", "image_generate", "text_to_speech",
    "skill_manage", "memory", "todo", "cronjob", "clarify",
    "process",
}


def classify_tool(tool_name: str) -> Tuple[bool, bool]:
    """Classify a tool as (is_read_only, is_concurrency_safe).

    Returns:
        (is_read_only, is_concurrency_safe) tuple.
        is_read_only: no side effects, safe to run in parallel.
        is_concurrency_safe: can be batched with other safe tools.
            Currently = is_read_only, but this split allows future
            refinement (e.g., idempotent writes could be concurrency-safe
            but not read-only).
    """
    # Check if tool adopted HermesTool base class (strong signal)
    try:
        from tools.tool_base import get_tool_instance
        tool = get_tool_instance(tool_name)
        if tool is not None:
            return (tool.is_read_only, tool.is_concurrency_safe)
    except Exception:
        pass

    # Fallback: conservative defaults
    is_read = tool_name in _READ_ONLY_DEFAULTS
    # Also check: not explicitly in mutating defaults
    if not is_read and tool_name not in _MUTATING_DEFAULTS:
        # Unknown tool → conservative: treat as mutating
        is_read = False

    return (is_read, is_read)  # concurrency_safe == read_only by default


def partition_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tool_name_key: str = "name",
) -> List[Dict[str, Any]]:
    """Partition tool calls into parallel-safe batches.

    Groups consecutive read-only tools into parallel batches.
    Each mutating tool gets its own batch (serial execution).

    Args:
        tool_calls: List of tool call dicts, each with a 'name' key
                    (or the key specified by tool_name_key).
        tool_name_key: Dict key containing the tool name.

    Returns:
        List of batches. Each batch is a dict:
            {"parallel": True/False, "calls": [...]}

    Example:
        Input: [read_file, grep, write_file, read_file]
        Output: [
            {parallel: True, calls: [read_file, grep]},
            {parallel: False, calls: [write_file]},
            {parallel: True, calls: [read_file]},
        ]
    """
    if not tool_calls:
        return []

    batches: List[Dict[str, Any]] = []
    current_batch: List[Dict[str, Any]] = []
    current_is_parallel = False

    def _flush() -> None:
        nonlocal current_batch
        if current_batch:
            batches.append({
                "parallel": current_is_parallel,
                "calls": list(current_batch),
            })
            current_batch = []

    for tc in tool_calls:
        name = tc.get(tool_name_key, "")
        _, is_safe = classify_tool(name)

        if current_batch and is_safe != current_is_parallel:
            _flush()

        current_batch.append(tc)
        current_is_parallel = is_safe

    _flush()
    return batches


def is_read_only_tool(tool_name: str) -> bool:
    """Quick check: is this tool safe for parallel execution?"""
    is_read, _ = classify_tool(tool_name)
    return is_read


def get_read_only_tool_names() -> Set[str]:
    """Return all known read-only tool names."""
    from tools.tool_base import get_read_only_tool_names as _base_names
    base = _base_names()
    if base:
        return base
    return set(_READ_ONLY_DEFAULTS)