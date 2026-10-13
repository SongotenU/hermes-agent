"""Invocation-surface flags: `user_invocable` / `model_invocable` frontmatter gates.

Each flag is an independent opt-out (default true), ported from DeepSeek Harness'
SkillInvocationPolicy:
- ``user_invocable: false``  -> no slash-command auto-registration (user surface).
- ``model_invocable: false`` -> hidden from the model index + skills_list, and
  ``skill_view`` refuses model-initiated loads; user-initiated loads
  (slash invocation, ``--skills`` preload, auto-load) still work.
"""

import json
from unittest.mock import patch

import pytest

import tools.skills_tool as skills_tool_module


@pytest.fixture(autouse=True)
def _clear_skill_caches():
    skills_tool_module._SKILLS_CACHE.clear()
    yield
    skills_tool_module._SKILLS_CACHE.clear()


def _make_skill(skills_dir, name, frontmatter_extra="", body="Do the thing."):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\nname: {name}\ndescription: Description for {name}.\n"
        f"{frontmatter_extra}---\n\n# {name}\n\n{body}\n"
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


class TestFlagParsing:
    def test_defaults_are_true(self):
        from agent.skill_utils import skill_model_invocable, skill_user_invocable

        assert skill_model_invocable({}) is True
        assert skill_user_invocable({}) is True

    def test_explicit_bools(self):
        from agent.skill_utils import skill_model_invocable, skill_user_invocable

        assert skill_user_invocable({"user_invocable": False}) is False
        assert skill_model_invocable({"model_invocable": True}) is True

    def test_string_values_from_fallback_parser(self):
        from agent.skill_utils import skill_model_invocable, skill_user_invocable

        assert skill_user_invocable({"user_invocable": "false"}) is False
        assert skill_model_invocable({"model_invocable": "No"}) is False
        assert skill_user_invocable({"user_invocable": "true"}) is True

    def test_unknown_or_missing_values_default_true(self):
        from agent.skill_utils import skill_user_invocable

        assert skill_user_invocable({"user_invocable": "banana"}) is True
        assert skill_user_invocable({"user_invocable": None}) is True


class TestSlashCommandSurface:
    def test_user_invocable_false_skips_slash_registration(self, tmp_path):
        from agent import skill_commands as sc

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "operator-only", frontmatter_extra="user_invocable: false\n")
            _make_skill(tmp_path, "normal")
            with patch.object(sc, "_skill_commands_by_key", {}):
                result = sc.scan_skill_commands()

        assert "/normal" in result
        assert "/operator-only" not in result

    def test_model_invocable_false_keeps_slash_registration(self, tmp_path):
        from agent import skill_commands as sc

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "model-hidden", frontmatter_extra="model_invocable: false\n")
            with patch.object(sc, "_skill_commands_by_key", {}):
                result = sc.scan_skill_commands()

        assert "/model-hidden" in result


class TestModelIndexSurface:
    def test_model_invocable_false_hidden_from_index(self, tmp_path, monkeypatch):
        from agent import prompt_builder as pb

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(pb, "get_disabled_skill_names", lambda *_: set())
        skills = tmp_path / "skills"
        pb.clear_skills_system_prompt_cache(clear_snapshot=True)
        try:
            _make_skill(skills, "model-hidden", frontmatter_extra="model_invocable: false\n")
            _make_skill(skills, "normal")
            text = pb._build_skills_system_prompt_inner(skills, [], None, None, None)
            assert "normal" in text
            assert "model-hidden" not in text
        finally:
            pb.clear_skills_system_prompt_cache(clear_snapshot=True)

    def test_hidden_flag_survives_snapshot_round_trip(self, tmp_path, monkeypatch):
        from agent import prompt_builder as pb

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(pb, "get_disabled_skill_names", lambda *_: set())
        skills = tmp_path / "skills"
        pb.clear_skills_system_prompt_cache(clear_snapshot=True)
        try:
            _make_skill(skills, "model-hidden", frontmatter_extra="model_invocable: false\n")
            first = pb._build_skills_system_prompt_inner(skills, [], None, None, None)
            assert "model-hidden" not in first
            # Drop only the in-process cache: the second build reuses the on-disk snapshot.
            pb.clear_skills_system_prompt_cache()
            from_snapshot = pb._build_skills_system_prompt_inner(skills, [], None, None, None)
            assert "model-hidden" not in from_snapshot
        finally:
            pb.clear_skills_system_prompt_cache(clear_snapshot=True)

    def test_user_invocable_false_still_in_index(self, tmp_path, monkeypatch):
        from agent import prompt_builder as pb

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(pb, "get_disabled_skill_names", lambda *_: set())
        skills = tmp_path / "skills"
        pb.clear_skills_system_prompt_cache(clear_snapshot=True)
        try:
            _make_skill(skills, "operator-only", frontmatter_extra="user_invocable: false\n")
            text = pb._build_skills_system_prompt_inner(skills, [], None, None, None)
            assert "operator-only" in text
        finally:
            pb.clear_skills_system_prompt_cache(clear_snapshot=True)


class TestExplicitUserLoads:
    def test_slash_invocation_loads_model_hidden_skill(self, tmp_path):
        from agent import skill_commands as sc

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "operator-only", frontmatter_extra="model_invocable: false\n")
            with patch.object(sc, "_skill_commands_by_key", {}):
                message = sc.build_skill_invocation_message("/operator-only")

        assert message is not None
        assert "Do the thing." in message


class TestSkillsListToolSurface:
    def test_skills_list_excludes_model_hidden(self, tmp_path):
        from tools.skills_tool import skills_list

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "model-hidden", frontmatter_extra="model_invocable: false\n")
            _make_skill(tmp_path, "normal")
            payload = json.loads(skills_list())

        names = {s["name"] for s in payload["skills"]}
        assert "normal" in names
        assert "model-hidden" not in names

    def test_skills_list_keeps_user_invocable_false(self, tmp_path):
        from tools.skills_tool import skills_list

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "operator-only", frontmatter_extra="user_invocable: false\n")
            payload = json.loads(skills_list())

        assert "operator-only" in {s["name"] for s in payload["skills"]}


class TestSkillViewToolGate:
    def test_model_initiated_view_refuses_user_only_skill(self, tmp_path):
        from tools.skills_tool import _skill_view_with_bump

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "operator-only", frontmatter_extra="model_invocable: false\n")
            payload = json.loads(_skill_view_with_bump({"name": "operator-only"}, task_id=None))

        assert payload["success"] is False
        assert "model_invocable" in payload["error"]

    def test_user_initiated_view_still_loads(self, tmp_path):
        from tools.skills_tool import skill_view

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            _make_skill(tmp_path, "operator-only", frontmatter_extra="model_invocable: false\n")
            payload = json.loads(skill_view("operator-only"))

        assert payload["success"] is True
        assert "Do the thing." in payload["content"]
