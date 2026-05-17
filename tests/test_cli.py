import json
import subprocess
import sys

import pytest

from tests.conftest import write_card, write_handoff, write_memory_index


def run_cli(args, env=None):
    cmd = [sys.executable, "-m", "memory_doctor.cli"] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


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
