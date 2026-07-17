"""Tests for git.py (pre-flight checks + commit driver)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import Mock

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


def test_working_tree_sane_detects_operation_in_linked_worktree(git_memory_dir, tmp_path):
    worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "worktree", "add", "-b", "linked", str(worktree)],
        check=True,
        capture_output=True,
        text=True,
    )
    git_dir = Path(
        subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    (git_dir / "MERGE_HEAD").write_text("deadbeef\n")

    ok, reason = working_tree_sane(worktree)

    assert ok is False
    assert "merge" in reason.lower()


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


def test_files_have_uncommitted_changes_parses_renamed_path(git_memory_dir):
    old = git_memory_dir / "old card.md"
    new = git_memory_dir / "new card.md"
    old.write_text("body\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "--", old.name], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "add card"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "mv", "--", old.name, new.name],
        check=True,
    )

    dirty = files_have_uncommitted_changes(git_memory_dir, [new])

    assert dirty == [(new, "staged")]


def test_files_have_uncommitted_changes_uses_nul_delimited_porcelain(
    git_memory_dir, monkeypatch
):
    target = git_memory_dir / "card with spaces.md"
    target.write_text("body\n")
    run = Mock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"?? card with spaces.md\0", stderr=b""
        )
    )
    monkeypatch.setattr("memory_doctor.git.subprocess.run", run)

    dirty = files_have_uncommitted_changes(git_memory_dir, [target])

    assert dirty == [(target, "untracked")]
    assert "-z" in run.call_args.args[0]
    assert run.call_args.kwargs["text"] is False


def test_files_have_uncommitted_changes_raises_status_stderr(git_memory_dir, monkeypatch):
    from memory_doctor import git as git_mod

    target = git_memory_dir / "card.md"
    target.write_text("body\n")
    monkeypatch.setattr(
        git_mod.subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                args=[], returncode=128, stdout=b"", stderr=b"fatal: status exploded\n"
            )
        ),
    )

    with pytest.raises(git_mod.GitStatusError, match="fatal: status exploded"):
        files_have_uncommitted_changes(git_memory_dir, [target])


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
        [
            "git", "-C", str(git_memory_dir), "log", "-1",
            "--format=%an <%ae>|%cn <%ce>",
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log == "Bob <bob@example.com>|Test <test@example.com>"


def test_commit_run_rejects_empty_author(git_memory_dir):
    f = git_memory_dir / "card-empty-author.md"
    f.write_text("x\n")

    result = commit_run(
        memory_dir=git_memory_dir,
        files=[f],
        subject="memory-doctor ingest: 1 handoff promoted",
        body="- card-empty-author.md",
        author="",
    )

    assert result.error_kind == "author"
    assert "missing name or email" in result.error_message
    status = subprocess.run(
        ["git", "-C", str(git_memory_dir), "status", "--porcelain", "--", str(f)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.startswith("??")


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


def test_is_git_repo_false_when_git_binary_missing(memory_dir, monkeypatch):
    # A git-less environment must degrade to "not a repo", not crash: plain
    # --apply now calls is_git_repo, which gates every other git helper.
    import subprocess as subprocess_mod
    from memory_doctor import git as git_mod

    def raise_missing(*args, **kwargs):
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(git_mod.subprocess, "run", raise_missing)
    assert git_mod.is_git_repo(memory_dir) is False
