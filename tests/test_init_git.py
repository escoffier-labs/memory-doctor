"""Tests for the init-git verb."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from memory_doctor.init_git import run as init_git_run
from memory_doctor.paths import PathConfig


def _make_cfg(memory_dir: Path, handoffs_dir: Path) -> PathConfig:
    return PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)


def test_init_git_creates_repo_and_initial_commit(memory_dir, handoffs_dir):
    # memory_dir has some seeded cards from the fixture. Initialize it.
    cfg = _make_cfg(memory_dir, handoffs_dir)
    rc = init_git_run(cfg)
    assert rc == 0
    assert (memory_dir / ".git").is_dir()

    log = subprocess.run(
        ["git", "-C", str(memory_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Exactly one commit.
    assert log.count("\n") == 1
    assert "memory: initial import" in log
    assert "cards" in log


def test_init_git_refuses_if_already_repo(git_memory_dir, handoffs_dir):
    cfg = _make_cfg(git_memory_dir, handoffs_dir)
    rc = init_git_run(cfg)
    assert rc == 2


def test_init_git_initial_commit_message_format(memory_dir, handoffs_dir):
    # Seed a card + MEMORY.md so the subject can mention both.
    (memory_dir / "card-one.md").write_text("# card-one\n")
    (memory_dir / "MEMORY.md").write_text("# Memory Index\n")
    cfg = _make_cfg(memory_dir, handoffs_dir)
    init_git_run(cfg)
    subject = subprocess.run(
        ["git", "-C", str(memory_dir), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Format: "memory: initial import (N cards, MEMORY.md)"
    assert subject.startswith("memory: initial import (")
    assert " cards" in subject
    assert "MEMORY.md" in subject


def test_init_git_writes_gitignore_atomically(memory_dir, handoffs_dir, monkeypatch):
    # Regression: .gitignore creation must go through atomic_write_text, not
    # raw Path.write_text (AGENTS.md hard rule for all file mutations).
    from memory_doctor import init_git as init_git_mod

    calls: list[Path] = []
    real = init_git_mod.atomic_write_text

    def spy(path: Path, content: str) -> None:
        calls.append(path)
        real(path, content)

    monkeypatch.setattr(init_git_mod, "atomic_write_text", spy)
    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))
    assert rc == 0
    assert memory_dir / ".gitignore" in calls
    assert (memory_dir / ".gitignore").read_text() == ""
