"""Git integration: pre-flight checks, commit driver, rollback.

All git interaction goes through subprocess.run(["git", ...]) with
capture_output=True and check=False. Callers branch on returncode and the
typed helpers in this module. Git status errors use a typed exception so
callers can fail closed while preserving Git's diagnostic.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitStatusError(RuntimeError):
    """Raised when Git cannot determine the requested files' status."""


def is_git_repo(memory_dir: Path) -> bool:
    """True if memory_dir is the toplevel of a git repo.

    Walking up to a parent repo is intentionally rejected: the memory dir
    must own its own .git/. We check by asking git for the toplevel and
    comparing it to the resolved memory_dir.
    """
    if not memory_dir.exists():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(memory_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        # No git binary on PATH: not a git repo as far as this tool can tell.
        # This gates ALL other git helpers (they are only called after a True
        # here), so a git-less environment degrades to non-repo behavior
        # instead of crashing.
        return False
    if result.returncode != 0:
        return False
    toplevel = Path(result.stdout.strip()).resolve()
    return toplevel == memory_dir.resolve()


def working_tree_sane(memory_dir: Path) -> tuple[bool, str]:
    """Refuse commits during merge / rebase / cherry-pick / bisect.

    Returns (True, "") when safe to commit; (False, reason) otherwise.
    The reason string is human-readable and surfaces in the CLI error.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(memory_dir), "rev-parse", "--absolute-git-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, f"unknown repository state: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return False, f"unknown repository state: {detail or 'git rev-parse failed'}"

    git_dir = Path(result.stdout.strip())
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
    cmd = [
        "git",
        "-C",
        str(memory_dir),
        "status",
        "--porcelain=v1",
        "-z",
        "--",
        *rel,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=False, check=False)
    except OSError as exc:
        raise GitStatusError(str(exc)) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", errors="replace").strip()
        raise GitStatusError(detail or f"git status failed with exit code {result.returncode}")

    dirty: list[tuple[Path, str]] = []
    files_by_rel = {str(f.resolve().relative_to(memory_dir.resolve())): f for f in files}
    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2].decode("ascii", errors="replace")
        path = record[3:].decode("utf-8", errors="surrogateescape")
        candidates = [path]
        if "R" in code or "C" in code:
            if index < len(records) and records[index]:
                candidates.append(
                    records[index].decode("utf-8", errors="surrogateescape")
                )
                index += 1
        file = next((files_by_rel[p] for p in candidates if p in files_by_rel), None)
        if file is None:
            continue
        if code == "??":
            status = "untracked"
        elif code[0] != " " and code[1] != " ":
            status = "modified, staged"
        elif code[0] != " ":
            status = "staged"
        else:
            status = "modified, not staged"
        dirty.append((file, status))
    return dirty


@dataclass
class CommitResult:
    """Outcome of a commit_run() invocation.

    On success: sha is set, error_kind is None.
    On failure: error_kind is one of {"add", "hook", "commit-other"};
    sha is None; error_message has the git stderr; staged_files lists
    what was already staged at the point of failure.
    """
    sha: str | None = None
    staged_files: list[Path] = field(default_factory=list)
    error_kind: str | None = None
    error_message: str | None = None


def commit_run(
    *,
    memory_dir: Path,
    files: list[Path],
    subject: str,
    body: str,
    author: str | None,
) -> CommitResult:
    """Stage `files` and create a commit with the given subject/body.

    Uses `git commit -- <files>` pathspec form so other staged content is
    not pulled into our commit. Author override applies only to the author;
    the committer continues to come from Git's configured identity.
    Never passes --no-verify; pre-commit hooks run normally.
    """
    if not files:
        return CommitResult()

    if author is not None:
        try:
            name, email = _parse_author(author)
        except ValueError as e:
            return CommitResult(error_kind="author", error_message=str(e))
    else:
        name = email = None

    rel = [str(f.resolve().relative_to(memory_dir.resolve())) for f in files]

    add_result = subprocess.run(
        ["git", "-C", str(memory_dir), "add", "--", *rel],
        capture_output=True, text=True, check=False,
    )
    if add_result.returncode != 0:
        return CommitResult(
            error_kind="add",
            error_message=add_result.stderr.strip() or add_result.stdout.strip(),
        )

    cmd = ["git", "-C", str(memory_dir), "commit", "--quiet"]
    if name and email:
        cmd.append(f"--author={name} <{email}>")
    cmd += ["-m", subject, "-m", body, "--", *rel]

    commit_result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if commit_result.returncode != 0:
        stderr = commit_result.stderr.lower()
        # Pre-commit hook failures vary by hook; git itself emits one of
        # these phrases when a hook exits non-zero. Any other failure is
        # bucketed as commit-other.
        hook_markers = ("pre-commit hook failed", "hook declined", "hook exited")
        if any(marker in stderr for marker in hook_markers):
            error_kind = "hook"
        else:
            error_kind = "commit-other"
        return CommitResult(
            staged_files=files,
            error_kind=error_kind,
            error_message=commit_result.stderr.strip() or commit_result.stdout.strip(),
        )

    sha_result = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
    return CommitResult(sha=sha, staged_files=files)


def _parse_author(spec: str) -> tuple[str, str]:
    """Parse 'Name <email>' into (name, email). Raises ValueError on bad format."""
    normalized = spec.strip()
    if not normalized:
        raise ValueError(f"author missing name or email: {spec!r}")
    if not spec.isprintable():
        raise ValueError("author must not contain control characters")
    if (
        normalized.count("<") != 1
        or normalized.count(">") != 1
        or not normalized.endswith(">")
    ):
        raise ValueError(f"author must be in 'Name <email>' format, got: {spec!r}")
    name_part, email_part = normalized.split("<", 1)
    name = name_part.strip()
    email = email_part[:-1]
    if not name or not email.strip():
        raise ValueError(f"author missing name or email: {spec!r}")
    if any(char.isspace() for char in email):
        raise ValueError("author email must not contain whitespace")
    if email.count("@") != 1:
        raise ValueError("author email must contain exactly one '@'")
    local, domain = email.split("@", 1)
    if not local or not domain:
        raise ValueError("author email must include text before and after '@'")
    return name, email


def validate_author_format(author: str | None) -> str | None:
    """Return an error message when author is not in 'Name <email>' format."""
    if author is None:
        return None
    try:
        _parse_author(author)
    except ValueError as e:
        return str(e)
    return None


def rollback_files(memory_dir: Path, files: list[Path]) -> None:
    """Best-effort revert each file to its HEAD state.

    For previously-tracked files: restore from `git show HEAD:<path>`.
    For new (untracked) files: delete from disk.
    Missing files are a no-op. Never raises; rollback failures are logged
    to stderr and swallowed, because rollback is itself an error-path call
    and we don't want to mask the original failure.
    """
    import sys
    for f in files:
        if not f.exists():
            continue
        rel = str(f.resolve().relative_to(memory_dir.resolve()))
        show = subprocess.run(
            ["git", "-C", str(memory_dir), "show", f"HEAD:{rel}"],
            capture_output=True, text=True, check=False,
        )
        if show.returncode == 0:
            try:
                f.write_text(show.stdout)
            except OSError as e:
                print(f"rollback: failed to restore {rel}: {e}", file=sys.stderr)
        else:
            # No HEAD version means the file was new; delete it.
            try:
                f.unlink()
            except OSError as e:
                print(f"rollback: failed to delete {rel}: {e}", file=sys.stderr)
