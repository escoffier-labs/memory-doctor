"""Tests for git.py (pre-flight checks + commit driver)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from memory_doctor.git import is_git_repo


def test_is_git_repo_true_when_initialized(git_memory_dir):
    assert is_git_repo(git_memory_dir) is True


def test_is_git_repo_false_when_not_initialized(memory_dir):
    # memory_dir fixture does NOT git init.
    assert is_git_repo(memory_dir) is False


def test_is_git_repo_false_when_nonexistent(tmp_path):
    assert is_git_repo(tmp_path / "does-not-exist") is False


from memory_doctor.git import working_tree_sane


def test_working_tree_sane_when_clean(git_memory_dir):
    ok, reason = working_tree_sane(git_memory_dir)
    assert ok is True
    assert reason == ""


def test_working_tree_sane_false_during_merge(git_memory_dir):
    # Simulate an in-progress merge by creating MERGE_HEAD.
    (git_memory_dir / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
    ok, reason = working_tree_sane(git_memory_dir)
    assert ok is False
    assert "merge" in reason.lower()


def test_working_tree_sane_false_during_rebase(git_memory_dir):
    (git_memory_dir / ".git" / "rebase-merge").mkdir()
    ok, reason = working_tree_sane(git_memory_dir)
    assert ok is False
    assert "rebase" in reason.lower()


def test_working_tree_sane_false_during_cherry_pick(git_memory_dir):
    (git_memory_dir / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n")
    ok, reason = working_tree_sane(git_memory_dir)
    assert ok is False
    assert "cherry-pick" in reason.lower()


def test_working_tree_sane_false_during_bisect(git_memory_dir):
    (git_memory_dir / ".git" / "BISECT_LOG").write_text("")
    ok, reason = working_tree_sane(git_memory_dir)
    assert ok is False
    assert "bisect" in reason.lower()


from memory_doctor.git import files_have_uncommitted_changes


def test_files_have_uncommitted_changes_clean(git_memory_dir):
    # Create + commit a file, then check it.
    f = git_memory_dir / "card-a.md"
    f.write_text("a\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", str(f)], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "add a"],
        check=True,
    )
    assert files_have_uncommitted_changes(git_memory_dir, [f]) == []


def test_files_have_uncommitted_changes_modified(git_memory_dir):
    f = git_memory_dir / "card-a.md"
    f.write_text("a\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", str(f)], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "add a"],
        check=True,
    )
    f.write_text("a modified\n")
    dirty = files_have_uncommitted_changes(git_memory_dir, [f])
    assert len(dirty) == 1
    assert dirty[0][0] == f
    assert "modified" in dirty[0][1].lower()


def test_files_have_uncommitted_changes_untracked(git_memory_dir):
    f = git_memory_dir / "card-new.md"
    f.write_text("new\n")
    dirty = files_have_uncommitted_changes(git_memory_dir, [f])
    assert len(dirty) == 1
    assert dirty[0][0] == f
    assert "untracked" in dirty[0][1].lower()


def test_files_have_uncommitted_changes_ignores_other_files(git_memory_dir):
    # Modifying a file we DON'T pass in should not show up.
    other = git_memory_dir / "other.md"
    other.write_text("other\n")
    target = git_memory_dir / "card-target.md"
    target.write_text("target\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "baseline2"],
        check=True,
    )
    other.write_text("other modified\n")
    assert files_have_uncommitted_changes(git_memory_dir, [target]) == []
