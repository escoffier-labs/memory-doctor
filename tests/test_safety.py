from pathlib import Path

import pytest

from memory_doctor.safety import UnsafeTargetError, resolve_card_target


def test_happy_path_flat_filename(memory_dir: Path):
    out = resolve_card_target(memory_dir, "foo.md")
    assert out == (memory_dir / "foo.md").resolve()


def test_strips_cards_prefix(memory_dir: Path):
    out = resolve_card_target(memory_dir, "cards/foo.md")
    assert out == (memory_dir / "foo.md").resolve()


def test_rejects_absolute_path(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "/etc/passwd.md")


def test_rejects_parent_traversal(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "../outside.md")


def test_rejects_traversal_after_cards_prefix(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "cards/../outside.md")


def test_rejects_nested_path(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "subdir/foo.md")


def test_rejects_empty_string(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "")
