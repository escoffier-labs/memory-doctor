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
