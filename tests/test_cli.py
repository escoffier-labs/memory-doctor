import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import write_card, write_handoff, write_memory_index


def run_cli(args, env=None):
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    src_path = str(Path(__file__).resolve().parents[1] / "src")
    existing = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = src_path if not existing else os.pathsep.join([src_path, existing])
    cmd = [sys.executable, "-m", "memory_doctor.cli"] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=child_env)


def test_status_default_dispatches(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["# Memory Index"])
    r = run_cli(["status", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 0
    assert "cards" in r.stdout.lower() or "memory dir" in r.stdout.lower()


def test_status_json_flag(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["# Memory Index"])
    r = run_cli(["status", "--json", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["cards"] == 0


def test_bad_verb_errors():
    r = run_cli(["frobnicate"])
    assert r.returncode == 2
    assert "frobnicate" in (r.stderr + r.stdout).lower() or "invalid choice" in r.stderr.lower()


def test_lint_exit_one_on_dead_links(memory_dir, handoffs_dir):
    write_card(memory_dir, "alpha", "[[ghost]]")
    r = run_cli(["lint", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 1
    assert "ghost" in r.stdout


def test_ingest_dry_run_default(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "h.md", action="create-card", target="x.md", content="x-body")
    r = run_cli(["ingest", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 0
    assert not (memory_dir / "x.md").exists()
    assert (handoffs_dir / "h.md").exists()


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


def test_env_commit_prints_activation_notice(memory_dir, handoffs_dir):
    write_handoff(
        handoffs_dir,
        "env-commit.md",
        action="create-card",
        target="env-commit.md",
        content="body",
    )

    result = run_cli(
        [
            "ingest",
            "--memory-dir",
            str(memory_dir),
            "--handoffs-dir",
            str(handoffs_dir),
        ],
        env={"MEMORY_DOCTOR_COMMIT": "1"},
    )

    assert result.returncode == 0
    assert (
        "notice: commit mode enabled by MEMORY_DOCTOR_COMMIT"
        in result.stderr
    )
    assert "use --no-commit to disable" in result.stderr


def test_no_commit_suppresses_env_activation_notice(memory_dir, handoffs_dir):
    write_handoff(
        handoffs_dir,
        "env-no-commit.md",
        action="create-card",
        target="env-no-commit.md",
        content="body",
    )

    result = run_cli(
        [
            "ingest",
            "--no-commit",
            "--memory-dir",
            str(memory_dir),
            "--handoffs-dir",
            str(handoffs_dir),
        ],
        env={"MEMORY_DOCTOR_COMMIT": "1"},
    )

    assert result.returncode == 0
    assert "commit mode enabled by MEMORY_DOCTOR_COMMIT" not in result.stderr
    assert "skipping commit" not in result.stdout


def test_explicit_commit_suppresses_env_activation_notice(memory_dir, handoffs_dir):
    result = run_cli(
        [
            "compact",
            "--commit",
            "--memory-dir",
            str(memory_dir),
            "--handoffs-dir",
            str(handoffs_dir),
        ],
        env={"MEMORY_DOCTOR_COMMIT": "1"},
    )

    assert result.returncode == 0
    assert "commit mode enabled by MEMORY_DOCTOR_COMMIT" not in result.stderr


@pytest.mark.parametrize(
    ("source", "author"),
    [
        ("cli", "Bad\nName <bad@example.com>"),
        ("env", "Bad\tName <bad@example.com>"),
        ("cli", "Bad>Name <bad@example.com>"),
        ("env", "Bad>Name <bad@example.com>"),
        ("cli", "Person <not-an-email>"),
        ("env", "Person <not-an-email>"),
        ("cli", "Person < user@example.com>"),
        ("env", "Person <user@example.com >"),
        ("cli", "Person <user @example.com>"),
    ],
)
def test_commit_author_rejects_unsafe_cli_and_env_values(
    git_memory_dir, handoffs_dir, source, author
):
    write_handoff(
        handoffs_dir,
        "unsafe-author.md",
        action="create-card",
        target="unsafe-author.md",
        content="body",
    )
    args = [
        "ingest",
        "--apply",
        "--memory-dir",
        str(git_memory_dir),
        "--handoffs-dir",
        str(handoffs_dir),
    ]
    env = {}
    if source == "cli":
        args.extend(["--commit", "--commit-author", author])
    else:
        env.update(
            {
                "MEMORY_DOCTOR_COMMIT": "1",
                "MEMORY_DOCTOR_COMMIT_AUTHOR": author,
            }
        )

    result = run_cli(args, env=env)

    assert result.returncode == 2
    assert "invalid --commit-author" in result.stderr
    assert not (git_memory_dir / "unsafe-author.md").exists()
    assert (handoffs_dir / "unsafe-author.md").exists()


@pytest.mark.parametrize("source", ["cli", "env"])
def test_commit_author_rejects_empty_cli_and_env_values(
    git_memory_dir, handoffs_dir, source
):
    fresh_handoffs_dir = handoffs_dir.parent / f"handoffs-without-processed-{source}"
    fresh_handoffs_dir.mkdir()
    write_handoff(
        fresh_handoffs_dir,
        "empty-author.md",
        action="create-card",
        target="empty-author.md",
        content="body",
    )
    args = [
        "ingest",
        "--apply",
        "--memory-dir",
        str(git_memory_dir),
        "--handoffs-dir",
        str(fresh_handoffs_dir),
    ]
    env = {}
    if source == "cli":
        args.extend(["--commit", "--commit-author", ""])
        env["MEMORY_DOCTOR_COMMIT_AUTHOR"] = "Environment Author <env@example.com>"
    else:
        env.update(
            {
                "MEMORY_DOCTOR_COMMIT": "1",
                "MEMORY_DOCTOR_COMMIT_AUTHOR": "",
            }
        )

    result = run_cli(args, env=env)

    assert result.returncode == 2
    assert "invalid --commit-author" in result.stderr
    assert not (git_memory_dir / "empty-author.md").exists()
    assert (fresh_handoffs_dir / "empty-author.md").exists()
    assert not (fresh_handoffs_dir / "processed").exists()
