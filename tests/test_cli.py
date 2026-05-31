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
