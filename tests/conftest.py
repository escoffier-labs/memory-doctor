"""Test fixtures: hermetic tmp_path-based memory + handoffs dirs."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

import pytest


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Empty memory dir at tmp_path/memory."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def git_memory_dir(memory_dir):
    """memory_dir + initialized git repo with an initial commit baseline.

    Lets tests exercise commit paths without re-running git init each test.
    Uses --quiet to keep test output clean.
    """
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(memory_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(memory_dir), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(memory_dir), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(memory_dir), "config", "commit.gpgsign", "false"],
        check=True,
    )
    subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(memory_dir), "commit", "--quiet", "--allow-empty", "-m", "baseline"],
        check=True,
    )
    return memory_dir


@pytest.fixture
def handoffs_dir(tmp_path: Path) -> Path:
    """Empty handoffs dir with processed/ subdir."""
    d = tmp_path / "handoffs"
    d.mkdir()
    (d / "processed").mkdir()
    return d


def write_card(memory_dir: Path, name: str, body: str, frontmatter: dict | None = None) -> Path:
    """Write a card to memory_dir/<name>.md."""
    path = memory_dir / f"{name}.md"
    parts: list[str] = []
    if frontmatter:
        parts.append("---")
        for k, v in frontmatter.items():
            parts.append(f"{k}: {v}")
        parts.append("---")
        parts.append("")
    parts.append(body)
    path.write_text("\n".join(parts))
    return path


def write_memory_index(memory_dir: Path, lines: Iterable[str]) -> Path:
    """Write MEMORY.md with the given lines."""
    path = memory_dir / "MEMORY.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_handoff(
    handoffs_dir: Path,
    name: str,
    *,
    action: str = "create-card",
    target: str = "new-card.md",
    content: str = "frontmatter and body go here",
) -> Path:
    """Write a handoff file matching the template format."""
    path = handoffs_dir / name
    text = (
        "# Memory Handoff\n\n"
        "## Type\nsetup\n\n"
        "## Title\nTest handoff\n\n"
        "## Summary\nSummary text.\n\n"
        "## Durable facts\n- Fact 1\n\n"
        f"## Recommended memory action\n{action}\n\n"
        f"## Target card\n{target}\n\n"
        f"## Suggested card content\n{content}\n"
    )
    path.write_text(text)
    return path
