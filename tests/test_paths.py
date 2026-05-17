import os
from pathlib import Path

import pytest

from memory_doctor.paths import (
    DEFAULT_MAX_LINES,
    PathConfig,
    resolve_paths,
    PathConfigError,
)


def test_resolves_flag_over_env_over_default(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", "/from-env")
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=None, max_lines=None)
    assert cfg.memory_dir == a


def test_resolves_env_when_no_flag(tmp_path, monkeypatch):
    a = tmp_path / "via-env"
    a.mkdir()
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", str(a))
    cfg = resolve_paths(memory_dir=None, handoffs_dir=None, max_lines=None)
    assert cfg.memory_dir == a


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
