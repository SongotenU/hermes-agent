"""One-visibility for delegated children: a tool outside the child's toolset is absent
from its schema AND refused at dispatch — with the same wording as a tool that never
existed — so a hallucinated call can never reach parent-only capability.

Port of DeepSeek Harness' tool-filter contract (a filtered tool is indistinguishable
from a nonexistent tool; the refusal happens at execute, not only in the prompt).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import model_tools
from agent.conversation_loop import _invalid_tool_name_error_content
from run_agent import AIAgent
from tools.delegate_tool_toolsets import _resolve_child_toolsets


class _Parent:
    """Minimal parent surface for the real child-toolset resolver."""

    enabled_toolsets = ["hermes-cli"]
    disabled_toolsets = ["browser"]


def _child_grant():
    return _resolve_child_toolsets(_Parent(), None, "leaf")


def _tool_call(name, call_id="c1", arguments="{}"):
    return SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(content="", finish_reason="stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model", usage=None,
    )


def _child_agent():
    """A real AIAgent carrying the child's actual toolset grant (same inputs
    ``_build_child_agent`` passes): schema and dispatch both scope to it."""
    child_toolsets, child_disabled = _child_grant()
    child_defs = model_tools.get_tool_definitions(
        enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled,
        quiet_mode=True, skip_tool_search_assembly=True)
    with (
        patch("model_tools.get_tool_definitions", return_value=child_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
            quiet_mode=True, skip_context_files=True, skip_memory=True,
            enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


class TestChildOneVisibility:
    def test_child_schema_excludes_blocked_tools(self):
        child_toolsets, child_disabled = _child_grant()
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=child_toolsets, disabled_toolsets=child_disabled,
            quiet_mode=True, skip_tool_search_assembly=True)
        names = {d["function"]["name"] for d in defs}
        # The child holds real work tools…
        assert names & {"terminal", "read_file", "web_search"}
        # …but never the blocked capability (recursion, scheduling in the parent's name).
        assert "delegate_task" not in names
        assert "cronjob_manage" not in names

    def test_hallucinated_out_of_scope_call_refused_at_dispatch(self):
        child = _child_agent()
        assert "delegate_task" not in child.valid_tool_names  # schema side

        bad = _response(content="", finish_reason="tool_calls",
                        tool_calls=[_tool_call("delegate_task", call_id="c1")])
        good = _response(content="Done", finish_reason="stop")
        child.client.chat.completions.create.side_effect = [bad, good]

        dispatched = []
        with (
            patch("model_tools.handle_function_call",
                  side_effect=lambda name, *a, **k: dispatched.append(name) or "ok"),
            patch.object(child, "_persist_session"),
            patch.object(child, "_save_trajectory"),
            patch.object(child, "_cleanup_task_resources"),
        ):
            result = child.run_conversation("delegate something")

        # Refused before dispatch: the call never executed.
        assert dispatched == []
        assert result["completed"] is True
        assert result["api_calls"] == 2

        refusals = [
            m for m in result["messages"]
            if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") == "c1"
        ]
        (refusal,) = refusals
        expected = f"Tool 'delegate_task' does not exist. Available tools: {', '.join(sorted(child.valid_tool_names))}"
        assert refusal["content"] == expected
        # Indistinguishable from a tool that never existed: no scope/permission hint
        # that would tell the model the capability exists but is withheld.
        assert "scope" not in refusal["content"].lower()
        assert "permission" not in refusal["content"].lower()

    def test_out_of_scope_and_nonexistent_refusals_share_one_wording(self):
        child = _child_agent()
        out_of_scope = _invalid_tool_name_error_content("delegate_task", child.valid_tool_names)
        nonexistent = _invalid_tool_name_error_content("totally_fake_tool_xyz", child.valid_tool_names)
        assert out_of_scope.startswith("Tool 'delegate_task' does not exist.")
        assert nonexistent.startswith("Tool 'totally_fake_tool_xyz' does not exist.")
        # Same template, same catalog — only the quoted name differs.
        assert out_of_scope.split("does not exist.")[1] == nonexistent.split("does not exist.")[1]
