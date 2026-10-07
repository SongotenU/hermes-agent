"""The recursive worktree guard must survive upstream's delegate-worktree rewrite.

A subagent that is itself running in an isolated worktree must not nest a
second one under it: its child's terminal would sit two levels deep from the
real checkout, and the inner ``worktree add`` runs against the outer worktree
rather than the repository the user is working in. Upstream moved this logic
into ``tools/subagent_worktree.py`` and the guard was dropped in the move.

These tests drive the real filesystem shape — a genuine ``git worktree add``,
where the worktree's ``.git`` is a FILE holding a ``gitdir:`` pointer, unlike
the main checkout's directory — because the file-vs-directory distinction is
the whole mechanism.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools import subagent_worktree


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def real_worktree(tmp_path):
    """A real git worktree of this repo, so ``.git`` is a file not a directory."""
    repo_root = subprocess.run(
        ["/usr/bin/git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    wt = tmp_path / "nested-probe"
    added = _git("worktree", "add", str(wt), "-b", "guard-probe-branch", "HEAD", cwd=repo_root)
    if added.returncode != 0:
        pytest.skip(f"git worktree add unavailable: {added.stderr.strip()[:120]}")
    try:
        yield wt
    finally:
        _git("worktree", "remove", "--force", str(wt), cwd=repo_root)
        _git("branch", "-D", "guard-probe-branch", cwd=repo_root)


class TestIsInsideWorktree:
    def test_main_checkout_is_not_a_worktree(self):
        repo_root = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not subagent_worktree.is_inside_worktree(repo_root)

    def test_real_worktree_is_detected(self, real_worktree):
        assert subagent_worktree.is_inside_worktree(str(real_worktree))

    def test_subdirectory_of_main_checkout_is_not_a_worktree(self):
        repo_root = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not subagent_worktree.is_inside_worktree(str(Path(repo_root) / "agent"))

    @pytest.mark.parametrize("value", [None, "", "/nonexistent/path/for/guard"])
    def test_non_paths_are_not_worktrees(self, value):
        assert not subagent_worktree.is_inside_worktree(value)


class TestRecursiveGuardBlocksCreation:
    def test_creation_is_skipped_when_parent_is_already_in_a_worktree(self, real_worktree):
        assert subagent_worktree.create_subagent_worktree(
            str(real_worktree), subagent_id="nested-guard"
        ) is None

    def test_creation_still_works_from_the_main_checkout(self):
        repo_root = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        info = subagent_worktree.create_subagent_worktree(repo_root, subagent_id="guard-ok")
        if info is None:
            pytest.skip("worktree creation unavailable in this environment (repo filters/env)")
        try:
            assert info["path"] and info["branch"]
        finally:
            subagent_worktree.finalize_subagent_worktree(info)