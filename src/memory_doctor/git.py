"""Git integration: pre-flight checks, commit driver, rollback.

All git interaction goes through subprocess.run(["git", ...]) with
capture_output=True and check=False. Callers branch on returncode and the
typed helpers in this module. No exceptions bubble up from subprocess.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def is_git_repo(memory_dir: Path) -> bool:
    """True if memory_dir is the toplevel of a git repo.

    Walking up to a parent repo is intentionally rejected: the memory dir
    must own its own .git/. We check by asking git for the toplevel and
    comparing it to the resolved memory_dir.
    """
    if not memory_dir.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False
    toplevel = Path(result.stdout.strip()).resolve()
    return toplevel == memory_dir.resolve()


def working_tree_sane(memory_dir: Path) -> tuple[bool, str]:
    """Refuse commits during merge / rebase / cherry-pick / bisect.

    Returns (True, "") when safe to commit; (False, reason) otherwise.
    The reason string is human-readable and surfaces in the CLI error.
    """
    git_dir = memory_dir / ".git"
    if (git_dir / "MERGE_HEAD").exists():
        return False, "merge in progress"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return False, "rebase in progress"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return False, "cherry-pick in progress"
    if (git_dir / "BISECT_LOG").exists():
        return False, "bisect in progress"
    return True, ""
