import errno
import os
import stat
from pathlib import Path

import pytest

from memory_doctor import safety
from memory_doctor.safety import UnsafeTargetError, atomic_write_text, resolve_card_target


def test_atomic_write_uses_utf8_when_locale_default_is_legacy(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "card.md"
    real_fdopen = os.fdopen

    def legacy_default(fd, mode, *args, **kwargs):
        if kwargs.get("encoding") is None:
            kwargs["encoding"] = "cp1252"
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(safety.os, "fdopen", legacy_default)

    atomic_write_text(path, "café — durable\n")

    assert path.read_bytes() == "café — durable\n".encode("utf-8")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX file modes")
def test_atomic_write_preserves_existing_mode(tmp_path: Path):
    path = tmp_path / "card.md"
    path.write_text("old")
    path.chmod(0o6750)

    atomic_write_text(path, "new")

    assert path.read_text() == "new"
    assert stat.S_IMODE(path.stat().st_mode) == 0o6750


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory fsync")
def test_atomic_write_syncs_file_then_replaces_then_syncs_directory(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "card.md"
    path.write_text("old")
    calls: list[str] = []
    real_replace = os.replace

    def record_fsync(fd: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        calls.append(f"fsync:{kind}")

    def record_replace(src: str, dst: Path) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(safety.os, "fsync", record_fsync)
    monkeypatch.setattr(safety.os, "replace", record_replace)

    atomic_write_text(path, "new")

    assert calls == ["fsync:file", "replace", "fsync:directory"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory fsync")
def test_atomic_write_ignores_unsupported_directory_fsync(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "card.md"
    path.write_text("old")
    real_fsync = os.fsync

    def reject_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        real_fsync(fd)

    monkeypatch.setattr(safety.os, "fsync", reject_directory_fsync)

    atomic_write_text(path, "new")

    assert path.read_text() == "new"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory fsync")
def test_atomic_write_propagates_directory_open_errors(tmp_path: Path, monkeypatch):
    path = tmp_path / "card.md"
    path.write_text("old")
    real_open = os.open

    def reject_directory_open(target, flags: int, mode: int = 0o777) -> int:
        if Path(target) == tmp_path:
            raise OSError(errno.EINVAL, "directory open failed")
        return real_open(target, flags, mode)

    monkeypatch.setattr(safety.os, "open", reject_directory_open)

    with pytest.raises(OSError, match="directory open failed"):
        atomic_write_text(path, "new")

    assert path.read_text() == "new"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory fsync")
def test_atomic_write_propagates_directory_fsync_io_errors(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "card.md"
    path.write_text("old")
    real_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(safety.os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        atomic_write_text(path, "new")

    assert path.read_text() == "new"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory fsync")
def test_atomic_write_does_not_unlink_temp_after_replacement(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "card.md"
    path.write_text("old")
    real_fsync = os.fsync
    unlink_calls: list[Path] = []

    def fail_directory_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        real_fsync(fd)

    def record_unlink(target) -> None:
        unlink_calls.append(Path(target))

    monkeypatch.setattr(safety.os, "fsync", fail_directory_fsync)
    monkeypatch.setattr(safety.os, "unlink", record_unlink)

    with pytest.raises(OSError, match="directory fsync failed"):
        atomic_write_text(path, "new")

    assert unlink_calls == []


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


def test_rejects_memory_index_as_target(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "MEMORY.md")


def test_rejects_memory_index_case_variants(memory_dir: Path):
    for variant in ("memory.md", "Memory.md", "MEMORY.MD"):
        with pytest.raises(UnsafeTargetError):
            resolve_card_target(memory_dir, variant)


def test_rejects_memory_index_with_cards_prefix(memory_dir: Path):
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "cards/MEMORY.md")


def test_rejects_git_metadata_targets(memory_dir: Path):
    for name in (".gitignore", ".git", ".gitattributes"):
        with pytest.raises(UnsafeTargetError):
            resolve_card_target(memory_dir, name)


def test_rejects_backslash_targets(memory_dir: Path):
    for name in (r"notes\.gitignore", r"sub\MEMORY.md", "a\\b.md"):
        with pytest.raises(UnsafeTargetError):
            resolve_card_target(memory_dir, name)


def test_rejects_control_and_format_characters(memory_dir: Path):
    for name in ("​MEMORY.md", "‮gitignore.md", "evil\x00.md", "​.gitignore"):
        with pytest.raises(UnsafeTargetError):
            resolve_card_target(memory_dir, name)


def test_rejects_symlink_to_memory_index(memory_dir: Path):
    (memory_dir / "MEMORY.md").write_text("index\n")
    trick = memory_dir / "trick.md"
    trick.symlink_to(memory_dir / "MEMORY.md")
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "trick.md")


def test_rejects_symlink_to_another_card(memory_dir: Path):
    (memory_dir / "real.md").write_text("real card\n")
    alias = memory_dir / "alias.md"
    alias.symlink_to(memory_dir / "real.md")
    with pytest.raises(UnsafeTargetError):
        resolve_card_target(memory_dir, "alias.md")
