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
