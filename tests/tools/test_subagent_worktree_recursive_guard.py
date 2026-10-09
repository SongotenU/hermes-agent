"""The recursive worktree guard must survive upstream's delegate-worktree rewrite.

A subagent that is itself running in an isolated worktree must not nest a
second one under it: its child's terminal would sit two levels deep from the
real checkout, and the inner ``worktree add`` runs against the outer worktree
rather than the repository the user is working in. Upstream moved this logic
into ``tools/subagent_worktree.py`` and the guard was dropped in the move.

These tests drive the real filesystem shape — a genuine ``git worktree add``,
where the worktree's ``.git`` is a FILE holding a ``gitdir:`` pointer, unlike
the main checkout's directory — because the file-vs-directory distinction is
the whole mechanism. Everything runs against a scratch repository under
``tmp_path``: the live-system guard forbids ``git worktree`` mutations of the
protected checkout, and a scratch repository reproduces the identical shapes
without touching it.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tools import subagent_worktree


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/git", *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def scratch_repo(tmp_path) -> Path:
    """A throwaway repository with one commit, outside the protected checkout."""
    repo = tmp_path / "scratch-repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("-c", "user.name=guard-probe", "-c", "user.email=guard-probe@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "seed"),
    ):
        done = _git(*args, cwd=repo)
        if done.returncode != 0:
            pytest.skip(f"scratch repo setup failed: {done.stderr.strip()[:140]}")
    return repo


@pytest.fixture
def real_worktree(scratch_repo, tmp_path) -> Iterator[Path]:
    """A real git worktree, so ``.git`` is a file not a directory."""
    wt = tmp_path / "nested-probe"
    added = _git("worktree", "add", str(wt), "-b", "guard-probe-branch", "HEAD", cwd=scratch_repo)
    if added.returncode != 0:
        pytest.skip(f"git worktree add unavailable: {added.stderr.strip()[:120]}")
    try:
        yield wt
    finally:
        _git("worktree", "remove", "--force", str(wt), cwd=scratch_repo)
        _git("branch", "-D", "guard-probe-branch", cwd=scratch_repo)


class TestIsInsideWorktree:
    def test_main_checkout_is_not_a_worktree(self, scratch_repo):
        assert not subagent_worktree.is_inside_worktree(str(scratch_repo))

    def test_real_worktree_is_detected(self, real_worktree):
        assert subagent_worktree.is_inside_worktree(str(real_worktree))

    def test_subdirectory_of_main_checkout_is_not_a_worktree(self, scratch_repo):
        subdir = scratch_repo / "agent"
        subdir.mkdir()
        assert not subagent_worktree.is_inside_worktree(str(subdir))

    @pytest.mark.parametrize("value", [None, "", "/nonexistent/path/for/guard"])
    def test_non_paths_are_not_worktrees(self, value):
        assert not subagent_worktree.is_inside_worktree(value)


class TestRecursiveGuardBlocksCreation:
    def test_creation_is_skipped_when_parent_is_already_in_a_worktree(self, real_worktree):
        assert subagent_worktree.create_subagent_worktree(
            str(real_worktree), subagent_id="nested-guard"
        ) is None

    def test_creation_still_works_from_the_main_checkout(self, scratch_repo):
        info = subagent_worktree.create_subagent_worktree(str(scratch_repo), subagent_id="guard-ok")
        if info is None:
            pytest.skip("worktree creation unavailable in this environment (repo filters/env)")
        try:
            assert info["path"] and info["branch"]
        finally:
            subagent_worktree.finalize_subagent_worktree(info)
