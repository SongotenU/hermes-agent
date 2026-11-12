"""Tests for tools.skills_revocations (the OpenClaude revocations port)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.skills_guard import content_digest_full
from tools.skills_revocations import (block_for_bundle, describe_entry, load_revocations,
                                      normalize_digest, revocation_match,
                                      revocations_path, verify_installed)

FULL = "sha256:" + "a" * 64


def _digest_of(dir_path: Path) -> str:
    return "sha256:" + content_digest_full(dir_path)


def _skill_dir(tmp_path: Path, name: str = "sample-skill", extra: str = "v1") -> Path:
    skill = tmp_path / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\ncontent {extra}")
    (skill / "helper.py").write_text("print('hi')\n")
    return skill


@pytest.fixture()
def list_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HERMES_SKILLS_REVOCATIONS", str(tmp_path / "revocations.json"))
    return tmp_path / "revocations.json"


def _write_list(list_file: Path, entries) -> None:
    list_file.write_text(json.dumps({"version": 1, "entries": entries}))


class TestPathResolution:
    def test_env_override_and_missing_file(self, list_file: Path, monkeypatch, tmp_path):
        assert revocations_path() == list_file
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-home"))
        assert load_revocations() == []

    def test_shapes(self, list_file: Path):
        for shape in ({"version": 1, "entries": [{"id": "x"}]},
                      [{"id": "x"}],
                      {"revocations": [{"id": "x"}]}):
            list_file.write_text(json.dumps(shape))
            assert load_revocations() == [{"id": "x"}]

    def test_malformed_fails_closed(self, list_file: Path):
        list_file.write_text("{not json")
        with pytest.raises(ValueError):
            load_revocations()

    def test_non_list_fails_closed(self, list_file: Path):
        list_file.write_text('{"entries": "oops"}')
        with pytest.raises(ValueError):
            load_revocations()


class TestMatchRule:
    def test_id_alone_covers_all_versions(self):
        entries = [{"id": "bad-skill", "reason": "malicious"}]
        for version in ("1.0.0", "9.9.9", None):
            assert revocation_match(entries, skill_id="bad-skill", version=version) == entries[0]
        assert revocation_match(entries, skill_id="other-skill", version="1.0.0") is None

    def test_id_plus_version_covers_one_release(self):
        entries = [{"id": "bad-skill", "version": "1.2.0"}]
        assert revocation_match(entries, skill_id="bad-skill", version="1.2.0") is not None
        assert revocation_match(entries, skill_id="bad-skill", version="1.3.0") is None
        assert revocation_match(entries, skill_id="bad-skill", version=None) is None

    def test_digest_exact_content(self, tmp_path):
        skill = _skill_dir(tmp_path, extra="sub-v3")
        entries = [{"digest": _digest_of(skill)}]
        assert revocation_match(entries, skill_id="whatever", version="1.0.0",
                                digest=_digest_of(skill)) is not None
        assert revocation_match(entries, skill_id="whatever", version="1.0.0",
                                digest=FULL) is None

    def test_source_needs_known_source(self):
        entries = [{"id": "s", "source": "official"}]
        assert revocation_match(entries, skill_id="s", source="official") is not None
        assert revocation_match(entries, skill_id="s", source="github") is None
        # Unknown source can never confirm an entry IF it names one — no match.
        assert revocation_match(entries, skill_id="s", source=None) is None

    def test_every_named_field_must_match(self):
        entries = [{"id": "s", "source": "github", "digest": FULL}]
        assert revocation_match(entries, skill_id="s", source="github", digest=FULL) is not None
        assert revocation_match(entries, skill_id="s", source="official", digest=FULL) is None
        assert revocation_match(entries, skill_id="s", source="github", digest="x") is None

    def test_malformed_entries_skipped(self):
        assert revocation_match([{"reason": "only prose"}, "junk", None],
                                skill_id="s") is None

    def test_normalize_digest(self):
        assert normalize_digest("  sha256:" + "A" * 16) == "a" * 16
        assert normalize_digest("z" * 16) == ""
        assert normalize_digest(None) == ""


class TestBlockForBundle:
    def test_file_id_block(self, list_file: Path, tmp_path):
        skill = _skill_dir(tmp_path / "b1", extra="b1")
        _write_list(list_file, [{"id": "sample-skill", "reason": "admin said no"}])
        blocked = block_for_bundle("sample-skill", "1.0.0", skill, source="github")
        assert blocked is not None and blocked["reason"] == "admin said no"
        assert block_for_bundle("clean-skill", "1.0.0", skill, source="github") is None

    def test_empty_list_no_block(self, list_file: Path, tmp_path):
        Path(list_file).write_text('{"version": 1, "entries": []}')
        assert block_for_bundle("anything", None, tmp_path) is None

    def test_digest_block(self, list_file: Path, tmp_path):
        skill = _skill_dir(tmp_path / "b2", extra="b2")
        _write_list(list_file, [{"digest": _digest_of(skill)}])
        assert block_for_bundle("renamed-skill", None, skill) is not None

    def test_malformed_list_blocks_hard(self, list_file: Path, tmp_path):
        Path(list_file).write_text("nope")
        blocked = block_for_bundle("anything", None, tmp_path, source="github")
        assert blocked is not None and "malformed" in blocked["reason"]

    def test_missing_file_no_block(self, tmp_path):
        assert block_for_bundle("anything", None, tmp_path, source="github") is None


class TestVerifyInstalled:
    def test_reports_revoked_and_clean(self, list_file: Path, tmp_path):
        skill = _skill_dir(tmp_path / "b3", extra="b3")
        _write_list(list_file, [{"id": "sample-skill", "reason": "do not use"}])
        rows = verify_installed(load_revocations(), [
            {"name": "sample-skill", "install_path": str(skill), "version": "1.0.0"},
            {"name": "clean-skill", "install_path": str(skill), "version": "1.0.0"},
        ])
        assert rows[0]["revoked"] is True and rows[0]["reason"] == "do not use"
        assert rows[1]["revoked"] is False

    def test_missing_install_path_without_id_entry(self, list_file: Path):
        _write_list(list_file, [{"digest": FULL}])
        rows = verify_installed(load_revocations(), [{"name": "gone-skill", "install_path": ""}])
        assert rows[0]["revoked"] is False  # digest entry can't be confirmed against a missing dir

    def test_missing_install_path_with_id_entry(self, list_file: Path):
        _write_list(list_file, [{"id": "gone-skill"}])
        rows = verify_installed(load_revocations(), [{"name": "gone-skill", "install_path": ""}])
        assert rows[0]["revoked"] is True  # id entry needs no digest


class TestDescribeEntry:
    def test_line_has_fields_and_reason(self):
        line = describe_entry({"id": "s", "version": "1.0.0", "reason": "why"})
        assert "id=s" in line and "version=1.0.0" in line and line.endswith("why")

    def test_line_without_fields(self):
        assert describe_entry({}) == "(no fields — malformed entry)"
