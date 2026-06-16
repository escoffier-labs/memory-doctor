# Memory-Doctor Git Integration Implementation Plan (Phase 1, v0.2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in git commits to `ingest --apply` and `compact --apply`, plus an `init-git` setup verb, so every memory-doctor write is reviewable and revertable.

**Architecture:** New `src/memory_doctor/git.py` module wraps `subprocess.run(["git", ...])` calls behind a typed interface (`is_git_repo`, `working_tree_sane`, `files_have_uncommitted_changes`, `commit_run`). New `init_git.py` verb mirrors the existing verb-module shape (`run(cfg, ...) -> int`). The `ingest` and `compact` verbs collect a list of touched files during their existing apply pass and hand it off to `commit_run` at the end. All git interaction is stdlib-only via subprocess; no new dependencies.

**Tech Stack:** Python 3.10+, stdlib only (`subprocess`, `pathlib`, `dataclasses`, `tempfile`), pytest for tests with `tmp_path` + `git init` fixtures.

**Spec:** `docs/superpowers/specs/2026-05-22-memory-doctor-git-integration-design.md`

---

## File Structure

**New files:**
- `src/memory_doctor/git.py`: pre-flight checks, commit driver, rollback. Single responsibility: shell out to git safely.
- `src/memory_doctor/init_git.py`: `init-git` verb. Single responsibility: bootstrap a memory dir as a git repo.
- `tests/test_git.py`: tests for `git.py`.
- `tests/test_init_git.py`: tests for `init_git.py`.

**Modified files:**
- `src/memory_doctor/cli.py`: add `--commit`/`--no-commit`/`--commit-author` flags to `ingest` and `compact` parsers; dispatch new `init-git` verb.
- `src/memory_doctor/ingest.py`: collect touched-file list during apply; after successful apply, optionally call `git.commit_run`.
- `src/memory_doctor/compact.py`: same pattern as `ingest.py`.
- `src/memory_doctor/__init__.py`: bump `__version__` to `0.2.0`.
- `tests/test_cli.py`: +1 test for `init-git` dispatch.
- `tests/test_ingest.py`: +2 tests for `--commit` integration.
- `tests/test_compact.py`: +2 tests for `--commit` integration.
- `README.md`: document new flags, env vars, and the `init-git` verb.
- `pyproject.toml`: bump version to `0.2.0`.

**Test fixture additions** (in `tests/conftest.py`): a `git_memory_dir` fixture builds on the existing `memory_dir` fixture and runs `git init` + initial commit so tests can exercise commit paths.

---

## Task 1: Add `git_memory_dir` fixture to conftest

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Read existing conftest to understand fixture style**

Run: `cat tests/conftest.py`

The existing `memory_dir`, `handoffs_dir`, `cfg` fixtures build a tmp memory dir. We need a sibling fixture that takes the resulting dir and turns it into a git repo with one committed baseline.

- [ ] **Step 2: Add the `git_memory_dir` fixture**

Append to `tests/conftest.py`:

```python
import subprocess


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
        ["git", "-C", str(memory_dir), "commit", "--quiet", "-m", "baseline"],
        check=True,
    )
    return memory_dir
```

- [ ] **Step 3: Smoke-run the fixture by adding a dummy test**

Add to a new file `tests/test_conftest_smoke.py` (delete after Task 1 completes):

```python
import subprocess


def test_git_memory_dir_fixture(git_memory_dir):
    result = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "baseline" in result.stdout
```

Run: `pytest tests/test_conftest_smoke.py -v`
Expected: 1 pass.

- [ ] **Step 4: Delete the smoke test file**

Run: `rm tests/test_conftest_smoke.py`

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add git_memory_dir fixture for commit-path tests"
```

---

## Task 2: Add `is_git_repo` to new `git.py` (TDD)

**Files:**
- Create: `src/memory_doctor/git.py`
- Create: `tests/test_git.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_git.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_git.py -v`
Expected: ImportError or 3 failures (module/function does not exist).

- [ ] **Step 3: Implement `is_git_repo`**

Create `src/memory_doctor/git.py`:

```python
"""Git integration: pre-flight checks, commit driver, rollback.

All git interaction goes through subprocess.run([\"git\", ...]) with
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
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_git.py -v`
Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/git.py tests/test_git.py
git commit -m "feat(git): add is_git_repo pre-flight check"
```

---

## Task 3: Add `working_tree_sane` (TDD)

**Files:**
- Modify: `src/memory_doctor/git.py`
- Modify: `tests/test_git.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_git.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_git.py -v`
Expected: 5 failures on ImportError for `working_tree_sane`.

- [ ] **Step 3: Implement `working_tree_sane`**

Append to `src/memory_doctor/git.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_git.py -v`
Expected: 8 passes total (3 from Task 2 + 5 new).

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/git.py tests/test_git.py
git commit -m "feat(git): refuse commits during merge/rebase/cherry-pick/bisect"
```

---

## Task 4: Add `files_have_uncommitted_changes` (TDD)

**Files:**
- Modify: `src/memory_doctor/git.py`
- Modify: `tests/test_git.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_git.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_git.py -v`
Expected: 4 new failures on ImportError.

- [ ] **Step 3: Implement `files_have_uncommitted_changes`**

Append to `src/memory_doctor/git.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_git.py -v`
Expected: 12 passes total.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/git.py tests/test_git.py
git commit -m "feat(git): detect uncommitted changes on target files"
```

---

## Task 5: Add `commit_run` happy path (TDD)

**Files:**
- Modify: `src/memory_doctor/git.py`
- Modify: `tests/test_git.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_git.py`:

```python
from memory_doctor.git import CommitResult, commit_run


def test_commit_run_happy_path(git_memory_dir):
    f = git_memory_dir / "card-new.md"
    f.write_text("hello\n")
    result = commit_run(
        memory_dir=git_memory_dir,
        files=[f],
        subject="memory-doctor ingest: 1 handoff promoted",
        body="- card-new.md (create-card)",
        author=None,
    )
    assert isinstance(result, CommitResult)
    assert result.error_kind is None
    assert result.sha is not None
    assert len(result.sha) >= 7

    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "-1", "--format=%s%n%n%b"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "memory-doctor ingest: 1 handoff promoted" in log
    assert "- card-new.md (create-card)" in log


def test_commit_run_with_author_override(git_memory_dir):
    f = git_memory_dir / "card-x.md"
    f.write_text("x\n")
    result = commit_run(
        memory_dir=git_memory_dir,
        files=[f],
        subject="memory-doctor ingest: 1 handoff promoted",
        body="- card-x.md",
        author="Bob <bob@example.com>",
    )
    assert result.error_kind is None
    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "-1", "--format=%an <%ae>"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log == "Bob <bob@example.com>"


def test_commit_run_no_ai_trailers(git_memory_dir):
    f = git_memory_dir / "card-y.md"
    f.write_text("y\n")
    commit_run(
        memory_dir=git_memory_dir,
        files=[f],
        subject="memory-doctor compact: 1 entry flattened",
        body="- card-y.md (appended)",
        author=None,
    )
    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "-1", "--format=%B"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Global commit-hygiene rule: no AI authorship trailers anywhere.
    assert "Co-Authored-By" not in log
    assert "Generated with" not in log
    assert "Created with" not in log


def test_commit_run_only_stages_listed_files(git_memory_dir):
    # Other unrelated unstaged work must not get pulled into our commit.
    target = git_memory_dir / "card-target.md"
    target.write_text("target\n")
    other = git_memory_dir / "card-other.md"
    other.write_text("other\n")
    commit_run(
        memory_dir=git_memory_dir,
        files=[target],
        subject="memory-doctor ingest: 1 handoff promoted",
        body="- card-target.md",
        author=None,
    )
    # other.md should still be untracked.
    status = subprocess.run(
        ["git", "-C", str(git_memory_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "card-other.md" in status
    assert "card-target.md" not in status  # already committed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_git.py -v`
Expected: 4 new failures on ImportError.

- [ ] **Step 3: Implement `commit_run` + `CommitResult`**

Append to `src/memory_doctor/git.py`:

```python
from dataclasses import dataclass, field


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
    not pulled into our commit. Author override via -c user.name/email.
    Never passes --no-verify; pre-commit hooks run normally.
    """
    if not files:
        return CommitResult()

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

    cmd = ["git", "-C", str(memory_dir)]
    if author:
        name, email = _parse_author(author)
        cmd += ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    cmd += ["commit", "--quiet", "-m", subject, "-m", body, "--", *rel]

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
    if "<" not in spec or not spec.rstrip().endswith(">"):
        raise ValueError(f"author must be in 'Name <email>' format, got: {spec!r}")
    name_part, email_part = spec.split("<", 1)
    name = name_part.strip()
    email = email_part.rstrip(">").strip()
    if not name or not email:
        raise ValueError(f"author missing name or email: {spec!r}")
    return name, email
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_git.py -v`
Expected: 16 passes total.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/git.py tests/test_git.py
git commit -m "feat(git): add commit_run driver for run-level commits"
```

---

## Task 6: Add commit_run failure handling + rollback (TDD)

**Files:**
- Modify: `src/memory_doctor/git.py`
- Modify: `tests/test_git.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_git.py`:

```python
from memory_doctor.git import rollback_files


def test_commit_run_hook_failure_leaves_staged(git_memory_dir):
    # Install a failing pre-commit hook.
    hooks_dir = git_memory_dir / ".git" / "hooks"
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'pre-commit hook failed'\nexit 1\n")
    hook.chmod(0o755)

    f = git_memory_dir / "card-hook.md"
    f.write_text("hook\n")
    result = commit_run(
        memory_dir=git_memory_dir,
        files=[f],
        subject="memory-doctor ingest: 1 handoff promoted",
        body="- card-hook.md",
        author=None,
    )
    assert result.error_kind == "hook"
    assert result.sha is None
    # File should remain on disk + staged (per spec section 4: don't auto-revert).
    assert f.exists()
    status = subprocess.run(
        ["git", "-C", str(git_memory_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Either "A " (added/staged) or "AM" if there were modifications.
    assert "card-hook.md" in status
    assert status.lstrip().startswith("A")


def test_rollback_files_reverts_modified(git_memory_dir):
    f = git_memory_dir / "card-z.md"
    f.write_text("original\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", str(f)], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "add z"],
        check=True,
    )
    f.write_text("modified\n")
    rollback_files(git_memory_dir, [f])
    assert f.read_text() == "original\n"


def test_rollback_files_deletes_untracked(git_memory_dir):
    f = git_memory_dir / "card-new.md"
    f.write_text("new\n")
    rollback_files(git_memory_dir, [f])
    assert not f.exists()


def test_rollback_files_safe_on_missing_file(git_memory_dir):
    # A file that was never written should silently no-op.
    rollback_files(git_memory_dir, [git_memory_dir / "never-existed.md"])
    # No assertion needed; just verify no exception.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_git.py -v`
Expected: 4 new failures (3 on missing `rollback_files`, 1 on hook handling if commit_run still succeeds in setup).

- [ ] **Step 3: Implement `rollback_files`**

Append to `src/memory_doctor/git.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_git.py -v`
Expected: 20 passes total.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/git.py tests/test_git.py
git commit -m "feat(git): add rollback_files for safe failure recovery"
```

---

## Task 7: Create `init-git` verb (TDD)

**Files:**
- Create: `src/memory_doctor/init_git.py`
- Create: `tests/test_init_git.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_init_git.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_init_git.py -v`
Expected: 3 failures on ImportError.

- [ ] **Step 3: Implement `init_git.py`**

Create `src/memory_doctor/init_git.py`:

```python
"""init-git verb: bootstrap a memory dir as a git repo with one initial commit."""
from __future__ import annotations

import subprocess
import sys

from memory_doctor.git import is_git_repo
from memory_doctor.paths import PathConfig


def run(cfg: PathConfig) -> int:
    memory_dir = cfg.memory_dir
    if not memory_dir.exists():
        print(f"memory-doctor init-git: memory dir does not exist: {memory_dir}", file=sys.stderr)
        return 2
    if is_git_repo(memory_dir):
        print(
            f"memory-doctor init-git: memory dir is already a git repo: {memory_dir}",
            file=sys.stderr,
        )
        return 2

    # Initialize with `main` as the default branch for predictability across
    # git versions; older defaults of `master` are inconsistent.
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(memory_dir)], check=True)

    # Reserve .gitignore as a customization point even though we don't
    # exclude anything yet. Operators frequently add `.DS_Store` or editor
    # backup patterns; better to have the file present than discover the
    # need later.
    gitignore = memory_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("")

    cards = sorted(p for p in memory_dir.glob("*.md") if p.name != "MEMORY.md")
    has_index = (memory_dir / "MEMORY.md").exists()
    pieces = [f"{len(cards)} cards"]
    if has_index:
        pieces.append("MEMORY.md")
    summary = ", ".join(pieces)
    subject = f"memory: initial import ({summary})"

    subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(memory_dir), "commit", "--quiet", "-m", subject],
        capture_output=True, text=True, check=False,
    )
    if commit.returncode != 0:
        print(
            f"memory-doctor init-git: initial commit failed: {commit.stderr.strip()}",
            file=sys.stderr,
        )
        return 2

    sha = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    print(f"memory-doctor init-git: initialized {memory_dir} at {sha}")
    return 0
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_init_git.py -v`
Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/init_git.py tests/test_init_git.py
git commit -m "feat(init-git): bootstrap memory dir as a git repo"
```

---

## Task 8: Wire `init-git` into CLI dispatch + add `--commit`/`--no-commit`/`--commit-author` flags

**Files:**
- Modify: `src/memory_doctor/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_init_git_verb_dispatches(memory_dir, handoffs_dir, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", str(memory_dir))
    monkeypatch.setenv("MEMORY_DOCTOR_HANDOFFS_DIR", str(handoffs_dir))
    from memory_doctor.cli import main
    rc = main(["init-git"])
    assert rc == 0
    assert (memory_dir / ".git").is_dir()


def test_ingest_commit_flag_parses(memory_dir, handoffs_dir, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", str(memory_dir))
    monkeypatch.setenv("MEMORY_DOCTOR_HANDOFFS_DIR", str(handoffs_dir))
    from memory_doctor.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["ingest", "--apply", "--commit", "--commit-author", "X <x@y.z>"])
    assert args.commit is True
    assert args.no_commit is False
    assert args.commit_author == "X <x@y.z>"


def test_compact_no_commit_flag_overrides_env(memory_dir, handoffs_dir):
    from memory_doctor.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["compact", "--apply", "--no-commit"])
    assert args.no_commit is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py -v`
Expected: 3 new failures.

- [ ] **Step 3: Modify `src/memory_doctor/cli.py`**

Replace the entire file with:

```python
"""Command-line interface for memory-doctor."""
from __future__ import annotations

import argparse
import os
import sys

from memory_doctor import __version__
from memory_doctor.paths import PathConfigError, resolve_paths


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memory-dir", default=None, help="Memory dir (cards + MEMORY.md).")
    p.add_argument("--handoffs-dir", default=None, help="Handoffs dir.")
    p.add_argument("--max-lines", type=int, default=None, help="MEMORY.md threshold (default 180)")


def _add_commit_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--commit", action="store_true", help="Stage + commit after --apply (off by default).")
    p.add_argument("--no-commit", action="store_true", help="Suppress committing even if MEMORY_DOCTOR_COMMIT=1.")
    p.add_argument(
        "--commit-author", default=None,
        help='Override author for this commit ("Name <email>"). Default: git config user.name/user.email.',
    )


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="memory-doctor", description="Maintenance CLI for the Claude Code / OpenClaw memory system.")
    root.add_argument("--version", action="version", version=f"memory-doctor {__version__}")
    sub = root.add_subparsers(dest="verb", required=True)

    p_status = sub.add_parser("status", help="Print a read-only summary")
    _add_common(p_status)
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of human text")

    p_lint = sub.add_parser("lint", help="Scan for dead [[wiki-links]]; exit 1 if any")
    _add_common(p_lint)

    p_ingest = sub.add_parser("ingest", help="Promote pending handoffs into cards")
    _add_common(p_ingest)
    _add_commit_flags(p_ingest)
    p_ingest.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")
    p_ingest.add_argument("--force", action="store_true", help="Overwrite existing cards on create-card conflict")

    p_compact = sub.add_parser("compact", help="Flatten multi-line MEMORY.md entries into topic files")
    _add_common(p_compact)
    _add_commit_flags(p_compact)
    p_compact.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")

    p_init = sub.add_parser("init-git", help="Initialize the memory dir as a git repo with one initial commit")
    _add_common(p_init)

    return root


def _resolve_commit_flag(args) -> bool:
    """Resolve commit intent from flag + env. --no-commit always wins."""
    if getattr(args, "no_commit", False):
        return False
    if getattr(args, "commit", False):
        return True
    return os.environ.get("MEMORY_DOCTOR_COMMIT", "").strip() in ("1", "true", "yes")


def _resolve_commit_author(args) -> str | None:
    return getattr(args, "commit_author", None) or os.environ.get("MEMORY_DOCTOR_COMMIT_AUTHOR") or None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = resolve_paths(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
        )
    except PathConfigError as e:
        print(f"memory-doctor: {e}", file=sys.stderr)
        return 2

    if args.verb == "status":
        from memory_doctor.status import run as run_status
        return run_status(cfg, as_json=args.json)
    if args.verb == "lint":
        from memory_doctor.lint import run as run_lint
        return run_lint(cfg)
    if args.verb == "ingest":
        from memory_doctor.ingest import run as run_ingest
        return run_ingest(
            cfg, apply=args.apply, force=args.force,
            commit=_resolve_commit_flag(args),
            commit_author=_resolve_commit_author(args),
        )
    if args.verb == "compact":
        from memory_doctor.compact import run as run_compact
        return run_compact(
            cfg, apply=args.apply,
            commit=_resolve_commit_flag(args),
            commit_author=_resolve_commit_author(args),
        )
    if args.verb == "init-git":
        from memory_doctor.init_git import run as run_init_git
        return run_init_git(cfg)
    parser.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_cli.py -v`
Expected: existing tests still pass + 3 new pass. Existing tests for ingest/compact may fail now because `run_ingest` and `run_compact` are called with new kwargs. Task 9 and Task 10 add those parameters.

If existing test_ingest/test_compact tests fail because of the new kwargs, that's expected and addressed in Task 9/10. To unblock immediately, temporarily add `commit=False, commit_author=None` defaults in ingest.py/compact.py signatures (Task 9/10 will replace with the real plumbing).

Apply this minimal stub now:

In `src/memory_doctor/ingest.py`, change the `run` signature to:

```python
def run(cfg: PathConfig, *, apply: bool = False, force: bool = False, commit: bool = False, commit_author: str | None = None) -> int:
```

In `src/memory_doctor/compact.py`, change the `run` signature to:

```python
def run(cfg: PathConfig, *, apply: bool = False, commit: bool = False, commit_author: str | None = None) -> int:
```

(Bodies unchanged; the new params are accepted but ignored until Task 9/10.)

Run: `pytest -q`
Expected: all existing tests pass + new CLI tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/cli.py src/memory_doctor/ingest.py src/memory_doctor/compact.py tests/test_cli.py
git commit -m "feat(cli): wire init-git verb + --commit/--no-commit/--commit-author flags"
```

---

## Task 9: Integrate `--commit` into the ingest verb (TDD)

**Files:**
- Modify: `src/memory_doctor/ingest.py`
- Modify: `tests/test_ingest.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py` (adjust imports at top of file as needed):

```python
def test_ingest_commit_creates_one_commit(git_memory_dir, handoffs_dir):
    # Use the existing handoff seeding pattern in this test module.
    # (See other tests in this file for the create-card handoff template format.)
    handoff = handoffs_dir / "h-commit-test.md"
    handoff.write_text(
        "# Handoff\n\n"
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\ncard-commit-test.md\n\n"
        "## Suggested card content\n"
        "---\nname: card-commit-test\n---\n\nbody\n"
    )

    from memory_doctor.ingest import run as ingest_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = ingest_run(cfg, apply=True, commit=True)
    assert rc == 0

    import subprocess
    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Baseline commit + one new commit from ingest.
    assert log.count("\n") == 2
    assert "memory-doctor ingest:" in log


def test_ingest_commit_refuses_when_not_git_repo(memory_dir, handoffs_dir):
    handoff = handoffs_dir / "h-norepo.md"
    handoff.write_text(
        "# Handoff\n\n"
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\ncard-norepo.md\n\n"
        "## Suggested card content\nbody\n"
    )
    from memory_doctor.ingest import run as ingest_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = ingest_run(cfg, apply=True, commit=True)
    assert rc == 2
    # File should not have been written.
    assert not (memory_dir / "card-norepo.md").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ingest.py -v`
Expected: 2 new failures (commit flag is accepted but no commit happens).

- [ ] **Step 3: Modify `src/memory_doctor/ingest.py`**

Replace the file with:

```python
"""Ingest verb: promote pending handoffs into cards."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from memory_doctor.git import (
    commit_run,
    files_have_uncommitted_changes,
    is_git_repo,
    working_tree_sane,
)
from memory_doctor.parsing import HandoffParseError, ParsedHandoff, parse_handoff
from memory_doctor.paths import PathConfig
from memory_doctor.safety import (
    UnsafeTargetError,
    atomic_write_text,
    resolve_card_target,
)


def _process_handoff(
    parsed: ParsedHandoff,
    memory_dir: Path,
    handoffs_dir: Path,
    *,
    apply: bool,
    force: bool,
    touched: list[tuple[Path, str]],
) -> tuple[str, bool]:
    """Returns (message, success). Appends (target_path, reason) to `touched`
    on each successful write so the caller can build the commit body."""
    src = parsed.path

    if parsed.action == "no-card":
        msg = f"{src.name}: no-card -> move to processed"
        if apply:
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    try:
        target = resolve_card_target(memory_dir, parsed.target)
    except UnsafeTargetError as e:
        return (f"{src.name}: SKIP - unsafe target {parsed.target!r}: {e}", False)

    if parsed.action == "create-card":
        if target.exists():
            existing = target.read_text()
            if existing.strip() == parsed.content.strip():
                msg = f"{src.name}: create-card -> {target.name} already identical, move to processed"
                if apply:
                    shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
                return msg, True
            if not force:
                return (f"{src.name}: SKIP - {target.name} exists with different content (use --force)", False)
            msg = f"{src.name}: create-card -> {target.name} (FORCE overwrite)"
            if apply:
                payload = parsed.content if parsed.content.endswith("\n") else parsed.content + "\n"
                atomic_write_text(target, payload)
                touched.append((target, f"create-card (force) from {src.name}"))
                shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
            return msg, True
        msg = f"{src.name}: create-card -> {target.name}"
        if apply:
            payload = parsed.content if parsed.content.endswith("\n") else parsed.content + "\n"
            atomic_write_text(target, payload)
            touched.append((target, f"create-card from {src.name}"))
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    if parsed.action == "update-card":
        if not target.exists():
            return (f"{src.name}: ERROR - update-card target {target.name} does not exist", False)
        msg = f"{src.name}: update-card -> {target.name} (append)"
        if apply:
            existing = target.read_text()
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            atomic_write_text(target, existing + sep + parsed.content + "\n")
            touched.append((target, f"update-card append from {src.name}"))
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    return (f"{src.name}: unknown action {parsed.action!r}", False)


def _preflight_for_commit(memory_dir: Path, planned_targets: list[Path]) -> int:
    """Run the three pre-flight checks from the spec. 0 = ok, 2 = abort.

    Called BEFORE any file write when --commit is set, so a failure leaves
    the on-disk state untouched.
    """
    if not is_git_repo(memory_dir):
        print(
            f"memory-doctor: --commit requires the memory dir to be a git repo\n"
            f"  memory dir: {memory_dir}\n"
            f"  fix: run `memory-doctor init-git` once, then retry",
            file=sys.stderr,
        )
        return 2

    ok, reason = working_tree_sane(memory_dir)
    if not ok:
        print(
            f"memory-doctor: refusing to commit, git is in the middle of a {reason}\n"
            f"  fix: complete or abort the in-progress operation, then retry",
            file=sys.stderr,
        )
        return 2

    dirty = files_have_uncommitted_changes(memory_dir, planned_targets)
    if dirty:
        print(
            "memory-doctor: refusing to commit, target files have uncommitted local changes:",
            file=sys.stderr,
        )
        for path, status in dirty:
            print(f"  - {path.name} ({status})", file=sys.stderr)
        print(
            "  fix: review with `git diff`, commit/stash/discard, then retry",
            file=sys.stderr,
        )
        return 2

    return 0


def _plan_targets(pending: list[Path], memory_dir: Path) -> list[Path]:
    """Resolve target file paths from each pending handoff for pre-flight checks.

    Skips handoffs that fail to parse or have unsafe targets; the main pass
    will report those properly. Pre-flight just needs the set of files that
    might be written.
    """
    targets: list[Path] = []
    for p in pending:
        try:
            parsed = parse_handoff(p)
        except HandoffParseError:
            continue
        if parsed.action == "no-card":
            continue
        try:
            targets.append(resolve_card_target(memory_dir, parsed.target))
        except UnsafeTargetError:
            continue
    return targets


def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    force: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    if commit and not apply:
        # Friendlier than erroring: people experimenting with the flag often
        # forget --apply, and the message guides them to the right thing.
        print("memory-doctor ingest: skipping commit (dry-run; use --apply)")

    if apply:
        (cfg.handoffs_dir / "processed").mkdir(exist_ok=True)

    pending = sorted(p for p in cfg.handoffs_dir.glob("*.md"))
    if not pending:
        print("memory-doctor ingest: no pending handoffs")
        return 0

    if apply and commit:
        planned = _plan_targets(pending, cfg.memory_dir)
        rc = _preflight_for_commit(cfg.memory_dir, planned)
        if rc != 0:
            return rc

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor ingest ({mode}): {len(pending)} handoff(s)")
    touched: list[tuple[Path, str]] = []
    all_ok = True
    promoted = 0
    skipped = 0
    for p in pending:
        try:
            parsed = parse_handoff(p)
        except HandoffParseError as e:
            print(f"  {p.name}: PARSE ERROR - {e}")
            all_ok = False
            skipped += 1
            continue
        msg, ok = _process_handoff(parsed, cfg.memory_dir, cfg.handoffs_dir, apply=apply, force=force, touched=touched)
        print(f"  {msg}")
        if ok:
            promoted += 1
        else:
            skipped += 1
            all_ok = False

    if apply and commit and touched:
        if skipped == 0:
            subject = f"memory-doctor ingest: {promoted} handoff{'s' if promoted != 1 else ''} promoted"
        else:
            subject = (
                f"memory-doctor ingest: {promoted} handoff{'s' if promoted != 1 else ''} promoted, "
                f"{skipped} skipped"
            )
        body = "\n".join(f"- {t.name} ({reason})" for t, reason in touched)
        result = commit_run(
            memory_dir=cfg.memory_dir,
            files=[t for t, _ in touched],
            subject=subject,
            body=body,
            author=commit_author,
        )
        if result.error_kind is None:
            print(f"\nCommitted {result.sha}")
        elif result.error_kind == "hook":
            print(
                "\nerror: pre-commit hook rejected the commit; your file changes are staged but not committed",
                file=sys.stderr,
            )
            print(f"  files: {', '.join(t.name for t, _ in touched)}", file=sys.stderr)
            print(f"  details: {result.error_message}", file=sys.stderr)
            return 1
        else:
            print(f"\nerror: commit failed ({result.error_kind}): {result.error_message}", file=sys.stderr)
            return 1
    elif apply and commit and not touched:
        print("\nno changes to commit")

    return 0 if all_ok else 1
```

- [ ] **Step 4: Run all tests**

Run: `pytest -q`
Expected: all pass, including the 2 new ingest commit tests.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/ingest.py tests/test_ingest.py
git commit -m "feat(ingest): integrate --commit with three pre-flight checks"
```

---

## Task 10: Integrate `--commit` into the compact verb (TDD)

**Files:**
- Modify: `src/memory_doctor/compact.py`
- Modify: `tests/test_compact.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compact.py`:

```python
def test_compact_commit_creates_one_commit(git_memory_dir, handoffs_dir):
    # Seed MEMORY.md over the threshold with a multi-line entry pointing
    # at a topic file that exists (matches the existing flatten test fixture pattern).
    topic = git_memory_dir / "topic-a.md"
    topic.write_text("# topic-a\n\nbody\n")
    import subprocess
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "add topic-a"],
        check=True,
    )

    # Build a MEMORY.md that exceeds max_lines=5 so compact triggers.
    index = git_memory_dir / "MEMORY.md"
    lines = [
        "# Memory Index",
        "",
        "## Section",
        "- [topic-a](topic-a.md) one-liner hook",
        "  detail-line-1",
        "  detail-line-2",
        "  detail-line-3",
    ]
    index.write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", str(index)], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "seed MEMORY.md"],
        check=True,
    )

    from memory_doctor.compact import run as compact_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=5)
    rc = compact_run(cfg, apply=True, commit=True)
    assert rc == 0

    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "memory-doctor compact:" in log
    # Subject should mention the line-count delta.
    subject = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert "MEMORY.md" in subject and "->" in subject


def test_compact_commit_skipped_when_no_changes(git_memory_dir, handoffs_dir):
    # MEMORY.md under threshold = no flatten = no commit.
    (git_memory_dir / "MEMORY.md").write_text("# tiny\n")
    import subprocess
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "tiny memory"],
        check=True,
    )
    baseline_count = subprocess.run(
        ["git", "-C", str(git_memory_dir), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    from memory_doctor.compact import run as compact_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = compact_run(cfg, apply=True, commit=True)
    assert rc == 0
    after_count = subprocess.run(
        ["git", "-C", str(git_memory_dir), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert after_count == baseline_count
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_compact.py -v`
Expected: 2 new failures.

- [ ] **Step 3: Modify `src/memory_doctor/compact.py`**

Update the `run` function (everything else stays the same). Replace the existing `run` function with:

```python
def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    import sys
    from memory_doctor.git import (
        commit_run,
        files_have_uncommitted_changes,
        is_git_repo,
        working_tree_sane,
    )

    if commit and not apply:
        print("memory-doctor compact: skipping commit (dry-run; use --apply)")

    index_path = cfg.memory_dir / "MEMORY.md"
    if not index_path.exists():
        print(f"memory-doctor compact: {index_path} does not exist")
        return 0

    plan = plan_compaction(cfg.memory_dir, cfg.max_lines)
    if plan.original_lines <= cfg.max_lines:
        print(f"memory-doctor compact: {plan.original_lines} lines <= {cfg.max_lines}, no action needed")
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor compact ({mode}): MEMORY.md {plan.original_lines} -> ~{plan.projected_lines} lines")

    if plan.unsafe_targets:
        print("\nWARNING: skipping entries with unsafe targets (path traversal / escapes memory dir):")
        for t in plan.unsafe_targets:
            print(f"  - {t}")

    if plan.missing_targets:
        print("\nERROR: target topic files missing for some flatten candidates:")
        for t in plan.missing_targets:
            print(f"  - {t}")
        print("\nRefusing to compact: would orphan content. Create the missing card(s) first.")
        return 2

    if not plan.flattens:
        print("\nNo multi-line entries to flatten. Manual archival of older sections is required.")
        return 0

    print("\nFlatten candidates:")
    for f in plan.flattens:
        print(f"  [{f.title}] -> {f.target_name} (+{len(f.detail_lines)} line(s))")

    if plan.projected_lines > cfg.max_lines:
        print(f"\nWARNING: even after flattening, MEMORY.md would be {plan.projected_lines} lines (still over {cfg.max_lines}).")
        print("Manual archival of older entries is required.")

    if not apply:
        return 0

    if commit:
        if not is_git_repo(cfg.memory_dir):
            print(
                f"memory-doctor: --commit requires the memory dir to be a git repo\n"
                f"  memory dir: {cfg.memory_dir}\n"
                f"  fix: run `memory-doctor init-git` once, then retry",
                file=sys.stderr,
            )
            return 2
        ok, reason = working_tree_sane(cfg.memory_dir)
        if not ok:
            print(
                f"memory-doctor: refusing to commit, git is in the middle of a {reason}\n"
                f"  fix: complete or abort the in-progress operation, then retry",
                file=sys.stderr,
            )
            return 2
        planned = [cfg.memory_dir / f.target_name for f in plan.flattens] + [index_path]
        dirty = files_have_uncommitted_changes(cfg.memory_dir, planned)
        if dirty:
            print(
                "memory-doctor: refusing to commit, target files have uncommitted local changes:",
                file=sys.stderr,
            )
            for path, status in dirty:
                print(f"  - {path.name} ({status})", file=sys.stderr)
            print("  fix: review with `git diff`, commit/stash/discard, then retry", file=sys.stderr)
            return 2

    _apply_flatten(cfg.memory_dir, plan)
    print(f"\nApplied. MEMORY.md now {plan.projected_lines} lines.")

    if not commit:
        return 0

    files = [cfg.memory_dir / f.target_name for f in plan.flattens] + [index_path]
    subject = (
        f"memory-doctor compact: {len(plan.flattens)} entr"
        f"{'ies' if len(plan.flattens) != 1 else 'y'} flattened, "
        f"MEMORY.md {plan.original_lines} -> {plan.projected_lines} lines"
    )
    body_lines = [
        f"- {f.target_name} (appended {len(f.detail_lines)}-line detail block from index)"
        for f in plan.flattens
    ]
    delta = plan.original_lines - plan.projected_lines
    body_lines.append(
        f"- MEMORY.md ({len(plan.flattens)} entries flattened to one-liners, -{delta} lines)"
    )
    body = "\n".join(body_lines)
    result = commit_run(
        memory_dir=cfg.memory_dir,
        files=files,
        subject=subject,
        body=body,
        author=commit_author,
    )
    if result.error_kind is None:
        print(f"\nCommitted {result.sha}")
        return 0
    if result.error_kind == "hook":
        print(
            "\nerror: pre-commit hook rejected the commit; your file changes are staged but not committed",
            file=sys.stderr,
        )
        print(f"  files: {', '.join(f.name for f in files)}", file=sys.stderr)
        print(f"  details: {result.error_message}", file=sys.stderr)
        return 1
    print(f"\nerror: commit failed ({result.error_kind}): {result.error_message}", file=sys.stderr)
    return 1
```

- [ ] **Step 4: Run all tests**

Run: `pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/memory_doctor/compact.py tests/test_compact.py
git commit -m "feat(compact): integrate --commit with three pre-flight checks"
```

---

## Task 11: Bump version and update README

**Files:**
- Modify: `src/memory_doctor/__init__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] **Step 1: Bump version**

In `src/memory_doctor/__init__.py`:

```python
__version__ = "0.2.0"
```

In `pyproject.toml`, change the `version` field:

```toml
version = "0.2.0"
```

- [ ] **Step 2: Update README**

Edit `README.md`. Find the verb table near the top and add `init-git`:

```
memory-doctor status              # read-only summary
memory-doctor lint                # find dead [[wiki-links]]; exit 1 if any
memory-doctor ingest [--apply]    # promote pending handoffs into cards
memory-doctor compact [--apply]   # flatten multi-line MEMORY.md entries into topic files
memory-doctor init-git            # initialize the memory dir as a git repo (one-time)
```

In the Configuration table, add two rows:

```
| Commit verb output             | --commit / --no-commit       | MEMORY_DOCTOR_COMMIT        | off                                              |
| Commit author override         | --commit-author "Name <e>"   | MEMORY_DOCTOR_COMMIT_AUTHOR | from git config                                  |
```

Add a new "Commit integration" section after the "What each verb does" section:

````markdown
## Commit integration (v0.2)

`ingest --apply` and `compact --apply` can be tied to a git commit in the memory dir so every write is reviewable and revertable.

```bash
# One-time setup: turn the memory dir into a git repo.
memory-doctor init-git

# Each --apply now produces one commit.
memory-doctor ingest --apply --commit
memory-doctor compact --apply --commit
```

Off by default; opt in via `--commit` or `MEMORY_DOCTOR_COMMIT=1`. `--no-commit` overrides the env var for a single run.

Pre-flight checks (any failure aborts the run, writes nothing):
1. Memory dir is a git repo (otherwise: `run memory-doctor init-git`).
2. No uncommitted local changes on the files this verb would touch (protects in-flight manual edits).
3. Git is not in the middle of a merge, rebase, cherry-pick, or bisect.

Commit message shape:

```
memory-doctor ingest: 3 handoffs promoted

- cards/foo.md (create-card from 2026-05-22_foo.md)
- cards/bar.md (update-card append from 2026-05-22_bar.md)
- cards/baz.md (create-card from 2026-05-22_baz.md)
```

No `Co-Authored-By` or `Generated with` trailers; subject already identifies the tool.

`--commit` without `--apply` is a no-op and exits 0 (friendly for experimentation).
````

- [ ] **Step 3: Run tests + version check**

Run: `pytest -q && python -c "from memory_doctor import __version__; print(__version__)"`
Expected: all tests pass, prints `0.2.0`.

- [ ] **Step 4: Commit**

```bash
git add src/memory_doctor/__init__.py pyproject.toml README.md
git commit -m "chore(release): bump to v0.2.0 (git integration)"
```

---

## Task 12: Final integration smoke test

**Files:** (no source changes; verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: ~81 tests pass (67 previous + ~14 new). Zero failures.

- [ ] **Step 2: Install locally and verify CLI surface**

Run:
```bash
pipx install --force ~/repos/memory-doctor
memory-doctor --version
memory-doctor --help
memory-doctor init-git --help
memory-doctor ingest --help
memory-doctor compact --help
```

Expected: `memory-doctor 0.2.0`, help shows `init-git` as a verb, `ingest`/`compact` show the new `--commit`/`--no-commit`/`--commit-author` flags.

- [ ] **Step 3: Dry-run smoke against real memory dir (read-only paths)**

Run: `memory-doctor status`

Expected: prints the standard status report against `~/.claude/projects/-home-alice/memory/` without exception. No commits made.

- [ ] **Step 4: Verify --commit refuses on the real memory dir**

The real memory dir is NOT a git repo. Run:

```bash
memory-doctor ingest --apply --commit
```

Expected: exits 2 with the "memory dir must be a git repo / run memory-doctor init-git" error. No file writes.

(Do NOT actually run `init-git` on the real memory dir as part of this task. That's an explicit operator decision, not a test step.)

- [ ] **Step 5: Final commit and merge prep**

Confirm the full diff on the branch:

```bash
git log master..spec/git-integration --oneline
```

Expected: ~13 commits (1 spec + 12 implementation tasks).

Push to GitHub for review:

```bash
git push -u origin spec/git-integration
```

(Do not open the PR automatically; the operator will choose whether to PR or merge locally.)

---

## Acceptance criteria recap (from spec)

1. `memory-doctor init-git` against a non-git memory dir creates a repo + initial commit. Idempotent: errors clearly if already a repo. → Task 7.
2. `memory-doctor ingest --apply --commit` against a git-tracked memory dir with pending handoffs produces exactly one commit with the documented subject + body, and moves the handoffs to `processed/`. → Task 9.
3. `memory-doctor compact --apply --commit` against an over-threshold MEMORY.md produces one commit whose subject includes the line-count delta. → Task 10.
4. Any of (not-a-repo, uncommitted target changes, mid-merge) aborts with exit 2 and writes nothing. → Tasks 2, 3, 4 (helpers) + 9, 10 (integration tests).
5. `--commit` without `--apply` prints "skipping commit (dry-run)" and exits 0. → Tasks 9, 10.
6. All new tests pass; existing 67 tests unchanged. → Task 12.
7. README documents `--commit`, `--no-commit`, `--commit-author`, the `init-git` verb, and the two new env vars. → Task 11.
