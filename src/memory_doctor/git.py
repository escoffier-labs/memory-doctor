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


def files_have_uncommitted_changes(
    memory_dir: Path, files: list[Path]
) -> list[tuple[Path, str]]:
    """Return (file, status_word) pairs for files with uncommitted changes.

    Empty list = all clean. status_word is human-readable
    ('modified', 'untracked', 'staged') and surfaces directly in the CLI error.
    """
    if not files:
        return []
    rel = [str(f.resolve().relative_to(memory_dir.resolve())) for f in files]
    cmd = ["git", "-C", str(memory_dir), "status", "--porcelain", "--", *rel]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # git status against a path inside an uninitialized repo would have
        # been caught upstream; treat unknown failure as "no dirty files
        # detected" rather than crash. The caller's is_git_repo() check is
        # the authoritative gate.
        return []

    dirty: list[tuple[Path, str]] = []
    files_by_rel = {str(f.resolve().relative_to(memory_dir.resolve())): f for f in files}
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        # Handle quoted paths from git status (paths with spaces or special chars).
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if path not in files_by_rel:
            continue
        if code == "??":
            status = "untracked"
        elif code[0] != " " and code[1] != " ":
            status = "modified, staged"
        elif code[0] != " ":
            status = "staged"
        else:
            status = "modified, not staged"
        dirty.append((files_by_rel[path], status))
    return dirty
