import os
from pathlib import Path

import pytest

from memory_doctor.paths import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    DEFAULT_MEMORY_DIR,
    PathConfig,
    _default_memory_dir,
    resolve_paths,
    PathConfigError,
)


def test_default_memory_dir_is_derived_from_home(monkeypatch):
    # The default should be computed per-user from $HOME, not hardcoded.
    monkeypatch.setenv("HOME", "/home/alice")
    assert _default_memory_dir() == "~/.claude/projects/-home-alice/memory"
    monkeypatch.setenv("HOME", "/home/bob")
    assert _default_memory_dir() == "~/.claude/projects/-home-bob/memory"


def test_default_memory_dir_matches_helper():
    # The module-level constant must agree with the helper at import time.
    assert DEFAULT_MEMORY_DIR == _default_memory_dir()


def test_resolves_flag_over_env_over_default(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    h = tmp_path / "handoffs"
    h.mkdir()
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", "/from-env")
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(h), max_lines=None)
    assert cfg.memory_dir == a


def test_resolves_env_when_no_flag(tmp_path, monkeypatch):
    a = tmp_path / "via-env"
    a.mkdir()
    h = tmp_path / "handoffs"
    h.mkdir()
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", str(a))
    cfg = resolve_paths(memory_dir=None, handoffs_dir=str(h), max_lines=None)
    assert cfg.memory_dir == a


def test_default_max_lines_matches_brigade_canonical():
    # brigade.budgets owns the canonical value; the in-module fallback must
    # agree with it so a degraded install behaves identically. Source-checkout
    # verification does not install runtime dependencies, so keep this parity
    # check optional while the local fallback tests below continue to run.
    budgets = pytest.importorskip("brigade.budgets")

    assert DEFAULT_MAX_LINES == budgets.MEMORY_INDEX_MAX_LINES == 180


def test_default_max_lines_180(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_DOCTOR_MAX_LINES", raising=False)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None)
    assert cfg.max_lines == DEFAULT_MAX_LINES == 180


def test_max_lines_override_via_flag(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=50)
    assert cfg.max_lines == 50


def test_max_lines_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MAX_LINES", "100")
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None)
    assert cfg.max_lines == 100


def test_invalid_max_lines_from_env_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MAX_LINES", "abc")
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    with pytest.raises(PathConfigError) as exc:
        resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None)
    assert "MEMORY_DOCTOR_MAX_LINES" in str(exc.value)


def test_non_positive_max_lines_raises(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    with pytest.raises(PathConfigError) as exc:
        resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=0)
    assert "greater than 0" in str(exc.value)


def test_default_max_bytes_is_24000(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_DOCTOR_MAX_BYTES", raising=False)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None, max_bytes=None)
    assert cfg.max_bytes == DEFAULT_MAX_BYTES == 24000


def test_max_bytes_override_via_flag(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None, max_bytes=5000)
    assert cfg.max_bytes == 5000


def test_max_bytes_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MAX_BYTES", "9000")
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None, max_bytes=None)
    assert cfg.max_bytes == 9000


def test_invalid_max_bytes_from_env_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MAX_BYTES", "abc")
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    with pytest.raises(PathConfigError) as exc:
        resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None, max_bytes=None)
    assert "MEMORY_DOCTOR_MAX_BYTES" in str(exc.value)


def test_non_positive_max_bytes_raises(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    with pytest.raises(PathConfigError) as exc:
        resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None, max_bytes=0)
    assert "greater than 0" in str(exc.value)


def test_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = tmp_path / "memory"; a.mkdir()
    b = tmp_path / "handoffs"; b.mkdir()
    cfg = resolve_paths(memory_dir="~/memory", handoffs_dir="~/handoffs", max_lines=None)
    assert cfg.memory_dir == a
    assert cfg.handoffs_dir == b


def test_missing_memory_dir_raises(tmp_path):
    b = tmp_path / "handoffs"; b.mkdir()
    with pytest.raises(PathConfigError) as exc:
        resolve_paths(memory_dir=str(tmp_path / "nope"), handoffs_dir=str(b), max_lines=None)
    assert "memory" in str(exc.value).lower()


def test_memory_dir_is_file_raises(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("file")
    b = tmp_path / "handoffs"; b.mkdir()
    with pytest.raises(PathConfigError):
        resolve_paths(memory_dir=str(f), handoffs_dir=str(b), max_lines=None)
