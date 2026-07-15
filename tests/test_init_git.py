"""Tests for the init-git verb."""
from __future__ import annotations

import shlex
import subprocess
import sys
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


def test_init_git_reports_init_failure_without_traceback(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import init_git as init_git_mod

    real_run = init_git_mod.subprocess.run

    def fail_init(args, **kwargs):
        if args[:2] == ["git", "init"]:
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="fatal: init exploded\n"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(init_git_mod.subprocess, "run", fail_init)

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 2
    err = capsys.readouterr().err
    assert "git init failed" in err
    assert "fatal: init exploded" in err
    assert "Traceback" not in err


def test_init_git_reports_add_failure_without_traceback(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import init_git as init_git_mod

    (memory_dir / "card.md").write_text("body\n")
    real_run = init_git_mod.subprocess.run

    def fail_add(args, **kwargs):
        if "add" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=128, stdout="", stderr="fatal: add exploded\n"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(init_git_mod.subprocess, "run", fail_add)

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 2
    err = capsys.readouterr().err
    assert "git add failed" in err
    assert "fatal: add exploded" in err
    assert "Traceback" not in err


def test_run_git_replaces_undecodable_diagnostics(monkeypatch):
    from memory_doctor import init_git as init_git_mod

    real_run = init_git_mod.subprocess.run

    def emit_invalid_utf8(args, **kwargs):
        return real_run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.buffer.write(b'fatal: invalid \\xff output\\n'); sys.exit(2)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(init_git_mod.subprocess, "run", emit_invalid_utf8)

    result = init_git_mod._run_git(["git", "status"])

    assert result is not None
    assert result.returncode == 2
    assert result.stderr == "fatal: invalid \ufffd output\n"


def test_init_git_refuses_unexpected_head_probe_failure(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import init_git as init_git_mod

    (memory_dir / "card.md").write_text("body\n")
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(memory_dir)], check=True)
    real_run_git = init_git_mod._run_git

    def fail_head_probe(args):
        if "rev-parse" in args and "--verify" in args and "HEAD" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=128,
                stdout="",
                stderr="fatal: corrupt HEAD\n",
            )
        return real_run_git(args)

    monkeypatch.setattr(init_git_mod, "_run_git", fail_head_probe)

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 2
    err = capsys.readouterr().err
    assert "git rev-parse HEAD failed" in err
    assert "fatal: corrupt HEAD" in err
    assert not (memory_dir / ".gitignore").exists()
    staged = subprocess.run(
        ["git", "-C", str(memory_dir), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert staged == []


def test_init_git_validates_nested_identity_before_creating_files(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    parent = memory_dir.parent
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(parent)], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.name", "Parent Identity"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "parent@example.com"],
        check=True,
    )
    empty_global_config = parent / "empty-gitconfig"
    empty_global_config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 2
    assert (memory_dir / ".git").is_dir()
    assert not (memory_dir / ".gitignore").exists()
    err = capsys.readouterr().err
    assert "user.name" in err
    assert "user.email" in err


def test_init_git_reports_config_command_errors(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import init_git as init_git_mod

    real_run = init_git_mod.subprocess.run

    def fail_config(args, **kwargs):
        if "config" in args and args[-1] == "user.name":
            return subprocess.CompletedProcess(
                args=args,
                returncode=128,
                stdout="",
                stderr="fatal: config backend exploded\n",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(init_git_mod.subprocess, "run", fail_config)

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 2
    err = capsys.readouterr().err
    assert "git config user.name failed" in err
    assert "fatal: config backend exploded" in err
    assert "Git identity is not configured" not in err


def test_init_git_stages_only_intended_top_level_files(memory_dir, handoffs_dir):
    (memory_dir / "card.md").write_text("card\n")
    (memory_dir / "MEMORY.md").write_text("index\n")
    (memory_dir / "private.txt").write_text("do not stage\n")
    nested = memory_dir / "nested"
    nested.mkdir()
    (nested / "note.md").write_text("do not stage\n")

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 0
    tracked = subprocess.run(
        ["git", "-C", str(memory_dir), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert tracked == [".gitignore", "MEMORY.md", "card.md"]


@pytest.mark.parametrize(
    ("invalid_path", "nested_path"),
    [
        ("archive.md", "secret.txt"),
        ("MEMORY.md", "secret.txt"),
        (".gitignore", "secret.txt"),
    ],
)
def test_init_git_refuses_non_file_initial_paths(
    memory_dir, handoffs_dir, capsys, invalid_path, nested_path
):
    invalid = memory_dir / invalid_path
    invalid.mkdir()
    (invalid / nested_path).write_text("do not stage\n")

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 2
    err = capsys.readouterr().err
    assert f"initial import path is not a regular file: {invalid}" in err
    staged = subprocess.run(
        ["git", "-C", str(memory_dir), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert staged == []
    head = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert head.returncode != 0
    assert not (memory_dir / ".gitignore").is_file()


def test_init_git_treats_card_names_as_literal_pathspecs(memory_dir, handoffs_dir):
    (memory_dir / ":!card.md").write_text("card\n")

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 0
    tracked = subprocess.run(
        ["git", "-C", str(memory_dir), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert tracked == [".gitignore", ":!card.md"]
    committed = subprocess.run(
        ["git", "-C", str(memory_dir), "show", "--format=", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert committed == [".gitignore", ":!card.md"]


def test_init_git_resumes_repository_without_initial_commit(memory_dir, handoffs_dir):
    (memory_dir / "card.md").write_text("body\n")
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(memory_dir)], check=True)

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    assert rc == 0
    subject = subprocess.run(
        ["git", "-C", str(memory_dir), "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert subject.startswith("memory: initial import")


def test_init_git_retry_after_commit_failure(memory_dir, handoffs_dir, monkeypatch):
    from memory_doctor import init_git as init_git_mod

    (memory_dir / "card.md").write_text("body\n")
    real_run = init_git_mod.subprocess.run
    failed = False

    def fail_first_commit(args, **kwargs):
        nonlocal failed
        if "commit" in args and not failed:
            failed = True
            return subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="hook declined\n"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr(init_git_mod.subprocess, "run", fail_first_commit)
    cfg = _make_cfg(memory_dir, handoffs_dir)

    assert init_git_run(cfg) == 2
    assert init_git_run(cfg) == 0


@pytest.mark.parametrize("failure", ["missing", "nonzero"])
def test_init_git_refuses_final_head_verification_failure(
    memory_dir, handoffs_dir, monkeypatch, capsys, failure
):
    from memory_doctor import init_git as init_git_mod

    real_run_git = init_git_mod._run_git

    def fail_final_head(args):
        if "rev-parse" in args and "--short=12" in args:
            if failure == "missing":
                print(
                    "memory-doctor init-git: could not run git: simulated failure",
                    file=sys.stderr,
                )
                return None
            return subprocess.CompletedProcess(
                args=args,
                returncode=128,
                stdout="",
                stderr="fatal: final HEAD verification failed\n",
            )
        return real_run_git(args)

    monkeypatch.setattr(init_git_mod, "_run_git", fail_final_head)

    rc = init_git_run(_make_cfg(memory_dir, handoffs_dir))

    captured = capsys.readouterr()
    assert rc == 2
    assert "initialized" not in captured.out
    head = subprocess.run(
        ["git", "-C", str(memory_dir), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head
    verify_command = shlex.join(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"]
    )
    status_command = shlex.join(["git", "-C", str(memory_dir), "status"])
    assert "initial commit may have succeeded" in captured.err
    assert f"`{verify_command}`" in captured.err
    assert f"`{status_command}`" in captured.err
    assert "then rerun `memory-doctor init-git`" not in captured.err
    if failure == "missing":
        assert "could not run git: simulated failure" in captured.err
    else:
        assert "final HEAD verification failed" in captured.err
        assert "fatal: final HEAD verification failed" in captured.err
