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
