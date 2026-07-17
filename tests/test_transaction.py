"""Tests for serialized, recoverable apply transactions."""
from __future__ import annotations

import copy
import errno
import json
import os
import stat
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from memory_doctor.safety import atomic_write_text
from memory_doctor.transaction import (
    ApplyTransaction,
    TransactionError,
    TransactionRecoveryError,
    _rename_noreplace,
    _state_key_for_resolved_path,
    has_pending_transaction_recovery,
    preflight_visible_path_aliases,
    preflight_transaction_capabilities,
)


def test_pending_recovery_probe_is_read_only(memory_dir):
    parent_before = {path.name for path in memory_dir.parent.iterdir()}

    assert has_pending_transaction_recovery(memory_dir) is False

    assert {path.name for path in memory_dir.parent.iterdir()} == parent_before


def test_pending_recovery_probe_allows_zero_inode_without_journal(
    memory_dir, monkeypatch
):
    real_stat = Path.stat

    def zero_memory_inode(path: Path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == memory_dir:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=0,
            )
        return result

    monkeypatch.setattr(Path, "stat", zero_memory_inode)

    assert has_pending_transaction_recovery(memory_dir) is False


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_pending_recovery_probe_translates_root_resolution_failure(
    memory_dir, monkeypatch, error_type
):
    real_resolve = Path.resolve

    def fail_memory_resolve(path: Path, *args, **kwargs):
        if path == memory_dir:
            raise error_type("root resolution failed")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_memory_resolve)

    with pytest.raises(TransactionRecoveryError, match="root resolution failed"):
        has_pending_transaction_recovery(memory_dir)


def test_pending_recovery_probe_detects_existing_journal(memory_dir):
    key = _state_key_for_resolved_path(memory_dir.resolve())
    state_dir = memory_dir.parent / f".memory-doctor-{key}"
    state_dir.mkdir()
    journal = state_dir / "apply.journal.json"

    assert has_pending_transaction_recovery(memory_dir) is False

    journal.write_text("{}")
    assert has_pending_transaction_recovery(memory_dir) is True


def test_pending_recovery_probe_reports_uninspectable_journal(
    memory_dir, monkeypatch
):
    key = _state_key_for_resolved_path(memory_dir.resolve())
    state_dir = memory_dir.parent / f".memory-doctor-{key}"
    state_dir.mkdir()
    journal = state_dir / "apply.journal.json"
    journal.write_text("{}")
    real_lstat = Path.lstat

    def fail_journal_lstat(path: Path):
        if path == journal:
            raise PermissionError("journal access denied")
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_journal_lstat)

    with pytest.raises(TransactionRecoveryError, match="journal access denied"):
        has_pending_transaction_recovery(memory_dir)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 test")
def test_rename_noreplace_never_clobbers_existing_destination(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("source\n")
    destination.write_text("destination\n")

    with pytest.raises(FileExistsError):
        _rename_noreplace(source, destination)

    assert source.read_text() == "source\n"
    assert destination.read_text() == "destination\n"


def test_rename_noreplace_windows_branch_preserves_file_exists(monkeypatch):
    from memory_doctor import transaction as transaction_mod

    source = Path("source")
    destination = Path("destination")

    def refuse_replace(actual_source, actual_destination):
        assert actual_source == source
        assert actual_destination == destination
        raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr(transaction_mod.os, "name", "nt")
    monkeypatch.setattr(transaction_mod.os, "rename", refuse_replace)

    with pytest.raises(FileExistsError):
        transaction_mod._rename_noreplace(source, destination)


def test_rename_noreplace_uses_darwin_renamex_np(monkeypatch):
    from memory_doctor import transaction as transaction_mod

    calls = []

    class NativeRename:
        argtypes = None
        restype = None

        def __call__(self, source, destination, flags):
            calls.append((source, destination, flags))
            return 0

    native = NativeRename()
    monkeypatch.setattr(transaction_mod.os, "name", "posix")
    monkeypatch.setattr(transaction_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        transaction_mod.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(renamex_np=native),
    )

    transaction_mod._rename_noreplace(Path("source"), Path("destination"))

    assert calls == [(b"source", b"destination", 0x00000004)]


def test_apply_transaction_serializes_same_memory_dir(memory_dir, handoffs_dir):
    first = ApplyTransaction(memory_dir, handoffs_dir)
    first.__enter__()
    attempting = threading.Event()
    acquired = threading.Event()

    def acquire_second() -> None:
        attempting.set()
        with ApplyTransaction(memory_dir, handoffs_dir) as second:
            acquired.set()
            second.commit()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert attempting.wait(1)
    assert acquired.wait(0.1) is False

    first.commit()
    first.__exit__(None, None, None)
    thread.join(timeout=2)
    assert acquired.is_set()


def test_apply_transaction_lock_does_not_depend_on_xdg_state_home(
    memory_dir, handoffs_dir, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(memory_dir.parent / "state-a"))
    first = ApplyTransaction(memory_dir, handoffs_dir)
    monkeypatch.setenv("XDG_STATE_HOME", str(memory_dir.parent / "state-b"))
    second = ApplyTransaction(memory_dir, handoffs_dir)

    first.__enter__()
    attempting = threading.Event()
    acquired = threading.Event()

    def acquire_second() -> None:
        attempting.set()
        with second:
            acquired.set()
            second.commit()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert attempting.wait(1)
    assert acquired.wait(0.1) is False

    first.commit()
    first.__exit__(None, None, None)
    thread.join(timeout=2)
    assert acquired.is_set()


def test_state_key_converges_case_and_unicode_normalization_aliases():
    composed = Path("/tmp/Mémory")
    decomposed_lower = Path("/tmp/me\u0301mory")

    assert _state_key_for_resolved_path(composed) == _state_key_for_resolved_path(
        decomposed_lower
    )


def test_alias_preflight_does_not_trust_case_insensitive_path_equality():
    class CaseInsensitivePath:
        def __init__(self, value: str):
            self.value = value

        def resolve(self, *, strict: bool = False):
            return self

        @property
        def name(self) -> str:
            return self.value.rsplit("/", 1)[-1]

        def __str__(self) -> str:
            return self.value

        def __fspath__(self) -> str:
            return self.value

        def __eq__(self, other) -> bool:
            return (
                isinstance(other, CaseInsensitivePath)
                and self.value.casefold() == other.value.casefold()
            )

    with pytest.raises(TransactionRecoveryError, match="may alias"):
        preflight_visible_path_aliases(
            [
                CaseInsensitivePath("C:/memory/Foo.md"),
                CaseInsensitivePath("C:/memory/foo.md"),
            ],
            label="test",
            check_existing_identities=False,
        )


def test_state_key_hash_collision_fails_closed_for_distinct_roots(
    tmp_path, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    first = tmp_path / "first-memory"
    second = tmp_path / "second-memory"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(
        transaction_mod,
        "_state_key_for_resolved_path",
        lambda path: "forced-collision",
    )
    card = first / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(first)
    abandoned.__enter__()
    abandoned.write_text(card, "transaction-created\n")
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="directory does not match"):
        ApplyTransaction(second).__enter__()

    assert card.read_text() == "transaction-created\n"
    assert abandoned._journal_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path semantics")
def test_normalized_unicode_root_collision_fails_closed_on_windows(tmp_path):
    composed = tmp_path / "Mémory"
    decomposed = tmp_path / "Me\N{COMBINING ACUTE ACCENT}mory"
    composed.mkdir()
    decomposed.mkdir()
    assert _state_key_for_resolved_path(
        composed.resolve()
    ) == _state_key_for_resolved_path(decomposed.resolve())
    card = composed / "card.md"
    card.write_text("original\n", encoding="utf-8")
    abandoned = ApplyTransaction(composed)
    abandoned.__enter__()
    abandoned.write_text(card, "transaction-created\n")
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="directory does not match"):
        ApplyTransaction(decomposed).__enter__()

    assert card.read_text(encoding="utf-8") == "transaction-created\n"
    assert abandoned._journal_path.exists()


def test_state_directory_reparse_point_is_rejected(
    memory_dir, handoffs_dir, monkeypatch
):
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction._state_dir.mkdir()
    real_lstat = Path.lstat

    def mark_state_reparse(path: Path):
        result = real_lstat(path)
        if path == transaction._state_dir:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=0x400,
            )
        return result

    monkeypatch.setattr(Path, "lstat", mark_state_reparse)

    with pytest.raises(TransactionRecoveryError, match="trusted local directory"):
        transaction.__enter__()

    assert transaction._lock_file is None


@pytest.mark.parametrize("child", ["journal", "lock"])
def test_private_state_child_symlink_is_rejected(
    memory_dir, handoffs_dir, tmp_path, child
):
    seed = ApplyTransaction(memory_dir, handoffs_dir)
    seed.__enter__()
    seed.commit()
    seed.__exit__(None, None, None)
    child_path = seed._journal_path if child == "journal" else seed._lock_path
    if child_path.exists():
        child_path.unlink()
    target = tmp_path / f"redirected-{child}"
    target.write_text("external\n")
    child_path.symlink_to(target)

    with pytest.raises(TransactionRecoveryError, match="trusted regular file"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert target.read_text() == "external\n"


def test_dangling_journal_symlink_is_rejected(memory_dir, handoffs_dir):
    seed = ApplyTransaction(memory_dir, handoffs_dir)
    seed.__enter__()
    seed.commit()
    seed.__exit__(None, None, None)
    seed._journal_path.symlink_to(seed._state_dir / "missing-journal-target")

    with pytest.raises(TransactionRecoveryError, match="trusted regular file"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert seed._journal_path.is_symlink()


def test_lock_acquisition_failure_closes_descriptor(
    memory_dir, handoffs_dir, monkeypatch
):
    transaction = ApplyTransaction(memory_dir, handoffs_dir)

    def fail_lock():
        raise OSError(errno.EIO, "lock acquisition failed")

    monkeypatch.setattr(transaction, "_acquire_lock", fail_lock)

    with pytest.raises(OSError, match="lock acquisition failed"):
        transaction.__enter__()

    assert transaction._lock_file is None


def test_windows_lock_acquisition_failure_does_not_unlock_unheld_byte(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.unlock_calls = 0

        def locking(self, fd, mode, count):
            del fd, count
            if mode == self.LK_NBLCK:
                raise OSError(errno.EIO, "lock acquisition failed")
            self.unlock_calls += 1
            raise OSError(errno.EIO, "unheld unlock masked original")

    fake = FakeMsvcrt()
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    monkeypatch.setattr(transaction_mod, "fcntl", None)
    monkeypatch.setattr(transaction_mod, "msvcrt", fake, raising=False)

    with pytest.raises(OSError, match="lock acquisition failed"):
        transaction.__enter__()

    assert fake.unlock_calls == 0
    assert transaction._lock_file is None
    assert transaction._lock_acquired is False


def test_windows_lock_retries_until_acquired(memory_dir, handoffs_dir, monkeypatch):
    from memory_doctor import transaction as transaction_mod

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = 0

        def locking(self, fd, mode, count):
            del fd, count
            if mode == self.LK_NBLCK:
                self.calls += 1
                if self.calls < 3:
                    raise OSError(errno.EACCES, "busy")

    fake = FakeMsvcrt()
    sleeps = []
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction._state_dir.mkdir()
    transaction._lock_file = transaction._lock_path.open("a+b")
    monkeypatch.setattr(transaction_mod, "fcntl", None)
    monkeypatch.setattr(transaction_mod, "msvcrt", fake, raising=False)
    monkeypatch.setattr(transaction_mod.time, "sleep", sleeps.append)
    try:
        transaction._acquire_lock()
    finally:
        transaction._lock_file.close()
        transaction._lock_file = None

    assert fake.calls == 3
    assert sleeps == [0.05, 0.05]


def test_mutation_capability_failure_precedes_journal_and_visible_write(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()

    def unsupported(source, destination):
        del source, destination
        raise OSError(errno.ENOTSUP, "rename unsupported")

    monkeypatch.setattr(transaction_mod, "_rename_noreplace", unsupported)

    with pytest.raises(TransactionRecoveryError, match="primitives are unavailable"):
        transaction.write_text(card, "changed\n")

    assert card.read_text() == "original\n"
    assert not transaction._journal_path.exists()
    assert not list(transaction._state_dir.glob(".memory-doctor-cap-*"))
    transaction._release_lock()


def test_side_effect_free_preflight_rejects_overlapping_roots(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()

    with pytest.raises(TransactionRecoveryError, match="roots overlap"):
        preflight_transaction_capabilities(shared, shared)

    assert list(tmp_path.iterdir()) == [shared]
    assert list(shared.iterdir()) == []


def test_mutation_capability_probe_cleans_private_artifacts(
    memory_dir, handoffs_dir
):
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()

    transaction.preflight_mutations()

    assert transaction._capabilities_checked is True
    assert not list(transaction._state_dir.glob(".memory-doctor-cap-*"))
    transaction.commit()
    transaction.__exit__(None, None, None)


def test_mutation_capability_probe_checks_each_configured_directory(
    memory_dir, handoffs_dir, monkeypatch
):
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    probed = []
    monkeypatch.setattr(transaction, "_probe_mutation_directory", probed.append)

    transaction.preflight_mutations()

    assert set(probed) == {
        transaction._state_dir,
        memory_dir,
        handoffs_dir,
        handoffs_dir / "processed",
    }
    transaction.commit()
    transaction.__exit__(None, None, None)


def test_processed_symlink_is_rejected_before_external_capability_probe(
    memory_dir, handoffs_dir, tmp_path
):
    processed = handoffs_dir / "processed"
    processed.rmdir()
    external = tmp_path / "external"
    external.mkdir()
    processed.symlink_to(external, target_is_directory=True)
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        with pytest.raises(TransactionRecoveryError, match="trusted local directory"):
            transaction.preflight_mutations()
    finally:
        transaction._release_lock()

    assert list(external.iterdir()) == []


def test_processed_parent_replacement_cannot_redirect_handoff_move(
    memory_dir, handoffs_dir, tmp_path, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    source = handoffs_dir / "pending.md"
    source.write_text("handoff\n")
    processed = handoffs_dir / "processed"
    destination = processed / source.name
    relocated = tmp_path / "relocated-processed"
    real_link = transaction_mod.os.link
    swapped = False

    def replace_parent_before_link(actual_source, actual_destination, **kwargs):
        nonlocal swapped
        if not swapped and actual_source == source:
            processed.rename(relocated)
            processed.mkdir()
            swapped = True
        return real_link(actual_source, actual_destination, **kwargs)

    monkeypatch.setattr(transaction_mod.os, "link", replace_parent_before_link)
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        with pytest.raises(TransactionRecoveryError, match=r"processed.*identity changed"):
            transaction.move_handoff(source, destination)
    finally:
        transaction._release_lock()

    assert source.read_text() == "handoff\n"
    assert list(relocated.iterdir()) == []
    assert list(processed.iterdir()) == []


def test_root_with_zero_inode_is_rejected(memory_dir, handoffs_dir, monkeypatch):
    real_stat = Path.stat

    def zero_memory_inode(path: Path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == memory_dir:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=0,
            )
        return result

    monkeypatch.setattr(Path, "stat", zero_memory_inode)

    with pytest.raises(TransactionRecoveryError, match="nonzero inode"):
        ApplyTransaction(memory_dir, handoffs_dir)


@pytest.mark.parametrize(
    ("file_attributes", "message"),
    [
        (0x1, "read-only"),
        (0x400, "not a regular file"),
    ],
)
def test_windows_unsafe_managed_file_is_rejected_before_journal(
    memory_dir, handoffs_dir, monkeypatch, file_attributes, message
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    real_lstat = Path.lstat

    def mark_unsafe(path: Path):
        result = real_lstat(path)
        if path == card:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
                st_file_attributes=file_attributes,
                st_reparse_tag=1 if file_attributes == 0x400 else 0,
            )
        return result

    monkeypatch.setattr(transaction_mod.os, "name", "nt")
    monkeypatch.setattr(Path, "lstat", mark_unsafe)
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        with pytest.raises(TransactionError, match=message):
            transaction.before_write(card)
    finally:
        transaction._release_lock()

    assert card.read_text() == "original\n"
    assert not transaction._journal_path.exists()


def test_managed_file_with_zero_inode_is_rejected(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    real_lstat = Path.lstat

    def zero_card_inode(path: Path):
        result = real_lstat(path)
        if path == card:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=0,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
            )
        return result

    monkeypatch.setattr(Path, "lstat", zero_card_inode)
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        with pytest.raises(TransactionError, match="nonzero inode"):
            transaction.before_write(card)
    finally:
        transaction._release_lock()

    assert card.read_text() == "original\n"
    assert not transaction._journal_path.exists()


def test_apply_transaction_recovers_abandoned_journal(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    atomic_write_text(card, "partial\n")
    abandoned.after_write(card)
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert card.read_text() == "original\n"
        recovered.commit()


def test_write_text_preserves_replacement_created_after_original_snapshot(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    expected = transaction.memory_file_identity(card)
    real_atomic_write = transaction_mod.atomic_write_text

    def replace_after_snapshot(path: Path, content: str, **kwargs):
        operator_path = memory_dir / "operator-card.tmp"
        operator_path.write_text("operator replacement\n")
        operator_path.replace(path)
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(
        transaction_mod,
        "atomic_write_text",
        replace_after_snapshot,
    )

    try:
        with pytest.raises(TransactionRecoveryError, match="changed after it was read"):
            transaction.write_text(
                card,
                "transaction replacement\n",
                expected_identity=expected,
            )
        assert card.read_text() == "operator replacement\n"
    finally:
        transaction._release_lock()


def test_write_text_preserves_replacement_created_after_quarantine(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    expected = transaction.memory_file_identity(card)
    real_rename = transaction_mod._rename_noreplace

    def collide_with_publish(source: Path, destination: Path):
        if destination == card and source.name.startswith(f".{card.name}."):
            operator_path = memory_dir / "operator-card.tmp"
            operator_path.write_text("operator replacement\n")
            operator_path.replace(card)
        return real_rename(source, destination)

    monkeypatch.setattr(
        transaction_mod,
        "_rename_noreplace",
        collide_with_publish,
    )

    try:
        with pytest.raises(TransactionRecoveryError, match="without clobbering"):
            transaction.write_text(
                card,
                "transaction replacement\n",
                expected_identity=expected,
            )
        record = transaction._files[card]
        assert card.read_text() == "operator replacement\n"
        assert record.quarantine.read_text() == "original\n"
        assert record.write_temp is not None
        assert record.write_temp.read_text() == "transaction replacement\n"
        assert transaction._journal_path.exists()
    finally:
        transaction._release_lock()


def test_apply_transaction_refuses_to_delete_recreated_new_file_during_recovery(
    memory_dir, handoffs_dir
):
    card = memory_dir / "new.md"
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    atomic_write_text(card, "partial\n")
    abandoned.after_write(card)
    abandoned._release_lock()

    atomic_write_text(card, "operator-recreated\n")

    with pytest.raises(TransactionRecoveryError, match="refusing to delete"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert card.read_text() == "operator-recreated\n"

    card.unlink()
    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        recovered.commit()


def test_apply_transaction_removes_new_file_during_immediate_rollback(
    memory_dir, handoffs_dir
):
    card = memory_dir / "new.md"
    with pytest.raises(RuntimeError, match="simulated failure"):
        with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
            transaction.before_write(card)
            atomic_write_text(card, "partial\n")
            transaction.after_write(card)
            raise RuntimeError("simulated failure")

    assert not card.exists()


def test_apply_transaction_preserves_replaced_new_file_on_immediate_rollback(
    memory_dir, handoffs_dir
):
    card = memory_dir / "new.md"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "changed\n")
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)
    atomic_write_text(card, "operator-replacement\n")

    with pytest.raises(TransactionRecoveryError, match="no longer matches"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "operator-replacement\n"

    card.unlink()
    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        recovered.commit()


def test_immediate_rollback_preserves_replacement_created_after_quarantine(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "new.md"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)
    record = transaction._files[card]
    real_rename = transaction_mod._rename_noreplace

    def replace_after_quarantine(source: Path, destination: Path):
        real_rename(source, destination)
        if source == card and destination.name.startswith(".mdq-"):
            atomic_write_text(card, "operator-replacement\n")

    monkeypatch.setattr(
        transaction_mod, "_rename_noreplace", replace_after_quarantine
    )

    with pytest.raises(TransactionRecoveryError, match="replacement collision"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "operator-replacement\n"
    assert record.quarantine.read_text() == "transaction-created\n"
    assert transaction._journal_path.exists()


def test_immediate_rollback_preserves_replaced_existing_file(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)
    atomic_write_text(card, "operator-replacement\n")

    with pytest.raises(TransactionRecoveryError, match="no longer matches"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "operator-replacement\n"


def test_crash_recovery_preserves_replaced_existing_file(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    abandoned.after_write(card)
    abandoned._release_lock()
    atomic_write_text(card, "operator-replacement\n")

    with pytest.raises(TransactionRecoveryError, match="no longer matches"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert card.read_text() == "operator-replacement\n"


def test_recovery_refuses_recreated_memory_root(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    abandoned.after_write(card)
    abandoned._release_lock()

    old_root = memory_dir.parent / "old-memory"
    memory_dir.rename(old_root)
    memory_dir.mkdir()
    replacement = memory_dir / "card.md"
    replacement.write_text("new-root-sentinel\n")

    with pytest.raises(TransactionRecoveryError, match="root identity"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert replacement.read_text() == "new-root-sentinel\n"


def test_recovery_refuses_recreated_handoffs_root(memory_dir, handoffs_dir):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    abandoned._release_lock()

    old_root = handoffs_dir.parent / "old-handoffs"
    handoffs_dir.rename(old_root)
    handoffs_dir.mkdir()
    (handoffs_dir / "processed").mkdir()
    replacement = handoffs_dir / "pending.md"
    replacement.write_text("new-root-sentinel\n")

    with pytest.raises(TransactionRecoveryError, match="handoffs root identity"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert replacement.read_text() == "new-root-sentinel\n"
    assert abandoned._journal_path.exists()


def test_recovery_without_handoffs_argument_validates_journaled_root(
    memory_dir, handoffs_dir
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    abandoned._release_lock()

    old_root = handoffs_dir.parent / "old-handoffs"
    handoffs_dir.rename(old_root)
    handoffs_dir.mkdir()
    (handoffs_dir / "processed").mkdir()
    replacement = handoffs_dir / "pending.md"
    replacement.write_text("new-root-sentinel\n")

    with pytest.raises(TransactionRecoveryError, match="handoffs root identity"):
        ApplyTransaction(memory_dir).__enter__()

    assert replacement.read_text() == "new-root-sentinel\n"
    assert abandoned._journal_path.exists()


def test_owned_new_file_unlink_is_synced_before_journal_clear(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import safety as safety_mod
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "new.md"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)
    calls: list[str] = []
    real_unlink = Path.unlink
    real_rename = transaction_mod._rename_noreplace

    def record_rename(source: Path, destination: Path):
        if source == card and destination.name.startswith(".mdq-"):
            calls.append("rename:quarantine")
        return real_rename(source, destination)

    def record_unlink(path: Path, *args, **kwargs):
        if path.name.startswith(".mdq-"):
            calls.append("unlink:quarantine")
        elif path == transaction._journal_path:
            calls.append("unlink:journal")
        return real_unlink(path, *args, **kwargs)

    def record_directory_sync(path: Path) -> None:
        if path == memory_dir:
            calls.append("fsync:memory")
        elif path == transaction._state_dir:
            calls.append("fsync:state")

    monkeypatch.setattr(Path, "unlink", record_unlink)
    monkeypatch.setattr(transaction_mod, "_rename_noreplace", record_rename)
    monkeypatch.setattr(
        transaction_mod, "_fsync_directory", record_directory_sync
    )
    monkeypatch.setattr(safety_mod, "_fsync_directory", record_directory_sync)

    transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert calls == [
        "rename:quarantine",
        "fsync:memory",
        "fsync:state",
        "fsync:state",
        "fsync:state",
        "unlink:quarantine",
        "fsync:memory",
        "unlink:journal",
        "fsync:state",
    ]


def test_first_state_directory_creation_syncs_parent(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    assert not transaction._state_dir.exists()
    calls: list[Path] = []

    monkeypatch.setattr(
        transaction_mod, "_fsync_directory", calls.append
    )

    transaction.__enter__()
    transaction.commit()
    transaction.__exit__(None, None, None)

    assert calls == [transaction._state_dir.parent, transaction._state_dir]


def test_apply_transaction_syncs_state_dir_after_journal_unlink(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "changed\n")
    calls: list[str] = []
    real_unlink = Path.unlink

    def record_unlink(path: Path, *args, **kwargs):
        if path == transaction._journal_path:
            calls.append("unlink:journal")
        return real_unlink(path, *args, **kwargs)

    def record_directory_sync(path: Path) -> None:
        if path == transaction._state_dir:
            calls.append("fsync:state")

    monkeypatch.setattr(Path, "unlink", record_unlink)
    monkeypatch.setattr(
        transaction_mod, "_fsync_directory", record_directory_sync, raising=False
    )

    transaction.commit()
    transaction.__exit__(None, None, None)

    assert calls == ["unlink:journal", "fsync:state"]


def test_committed_marker_sync_failure_never_rolls_back(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import safety as safety_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "changed\n")

    state_syncs = 0

    def fail_first_state_sync(path: Path) -> None:
        nonlocal state_syncs
        if path == transaction._state_dir:
            state_syncs += 1
        if path == transaction._state_dir and state_syncs == 1:
            raise OSError(errno.EIO, "state directory fsync failed")

    monkeypatch.setattr(
        safety_mod, "_fsync_directory", fail_first_state_sync, raising=False
    )

    with pytest.raises(TransactionRecoveryError, match="indeterminate"):
        transaction.commit()
    assert transaction.committed is True
    assert json.loads(transaction._journal_path.read_text())["phase"] == "committed"
    transaction.__exit__(None, None, None)

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "changed\n"
        recovered.commit()

    assert card.read_text() == "changed\n"
    assert state_syncs >= 1


def test_unreadable_journal_after_marker_failure_never_rolls_back(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import safety as safety_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "changed\n")
    failed_sync = False
    failed_read = False
    real_read_text = Path.read_text

    def fail_marker_sync(path: Path) -> None:
        nonlocal failed_sync
        if path == transaction._state_dir and not failed_sync:
            failed_sync = True
            raise OSError(errno.EIO, "marker sync failed")

    def fail_commit_reread(path: Path, *args, **kwargs):
        nonlocal failed_read
        if path == transaction._journal_path and failed_sync and not failed_read:
            failed_read = True
            raise OSError(errno.EIO, "journal unreadable")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(safety_mod, "_fsync_directory", fail_marker_sync)
    monkeypatch.setattr(Path, "read_text", fail_commit_reread)

    with pytest.raises(TransactionRecoveryError, match="indeterminate"):
        transaction.commit()

    assert transaction.committed is True
    transaction.__exit__(None, None, None)
    assert card.read_text() == "changed\n"
    assert transaction._journal_path.exists()


def test_invalid_active_journal_after_marker_failure_never_rolls_back(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "changed\n")
    real_write_journal = transaction._write_journal

    def fail_committed_marker() -> None:
        if transaction._phase != "committed":
            real_write_journal()
            return
        atomic_write_text(
            transaction._journal_path,
            json.dumps({"version": 2, "phase": "active"}),
        )
        raise OSError(errno.EIO, "committed marker write failed")

    monkeypatch.setattr(transaction, "_write_journal", fail_committed_marker)

    with pytest.raises(TransactionRecoveryError, match="indeterminate"):
        transaction.commit()

    assert transaction.committed is True
    transaction.__exit__(None, None, None)
    assert card.read_text() == "changed\n"
    assert transaction._journal_path.exists()


def test_valid_active_journal_after_marker_failure_allows_rollback(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "changed\n")
    real_write_journal = transaction._write_journal

    def fail_committed_marker() -> None:
        if transaction._phase != "committed":
            real_write_journal()
            return
        raise OSError(errno.EIO, "committed marker write failed")

    monkeypatch.setattr(transaction, "_write_journal", fail_committed_marker)

    with pytest.raises(OSError, match="committed marker write failed"):
        transaction.commit()

    assert transaction.committed is False
    monkeypatch.setattr(transaction, "_write_journal", real_write_journal)
    transaction.__exit__(None, None, None)
    assert card.read_text() == "original\n"
    assert not transaction._journal_path.exists()


def test_apply_transaction_restores_abandoned_handoff_move(memory_dir, handoffs_dir):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    os.replace(source, destination)
    abandoned.after_move(source, destination)
    abandoned._release_lock()

    with ApplyTransaction(memory_dir) as recovered:
        assert source.read_text() == "handoff\n"
        assert not destination.exists()
        recovered.commit()


def test_move_recovery_refuses_when_both_paths_are_missing(memory_dir, handoffs_dir):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    source.unlink()
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="both source and destination are missing"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert abandoned._journal_path.exists()


def test_move_recovery_preserves_replaced_destination(memory_dir, handoffs_dir):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    os.replace(source, destination)
    abandoned.after_move(source, destination)
    abandoned._release_lock()
    atomic_write_text(destination, "operator-replacement\n")

    with pytest.raises(TransactionRecoveryError, match="no longer matches"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert destination.read_text() == "operator-replacement\n"
    assert not source.exists()


def test_move_recovery_preserves_expected_destination_when_source_was_replaced(
    memory_dir, handoffs_dir
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff-original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    os.replace(source, destination)
    abandoned.after_move(source, destination)
    atomic_write_text(source, "operator-replacement\n")
    record = abandoned._moves[0]
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="replaced|resolve manually"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert source.read_text() == "operator-replacement\n"
    preserved = [
        path
        for path in (destination, record.destination_quarantine)
        if path.exists() and path.read_text() == "handoff-original\n"
    ]
    assert preserved, "different source + expected destination deleted the last original"
    assert abandoned._journal_path.exists()


def test_file_rollback_retains_private_original_when_restored_path_is_replaced(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "transaction-created\n")
    record = transaction._files[card]
    real_restore = transaction._restore_original_exclusive

    def replace_after_restore(actual_record):
        real_restore(actual_record)
        atomic_write_text(card, "operator-replacement\n")

    monkeypatch.setattr(
        transaction, "_restore_original_exclusive", replace_after_restore
    )

    with pytest.raises(TransactionRecoveryError, match="changed|replacement"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "operator-replacement\n"
    assert record.restore_temp.read_text() == "original\n"
    assert transaction._journal_path.exists()


def test_move_rollback_retains_original_when_restored_source_is_replaced(
    memory_dir, handoffs_dir, monkeypatch
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff-original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    os.replace(source, destination)
    abandoned.after_move(source, destination)
    record = abandoned._moves[0]
    real_quarantine_destination = abandoned._quarantine_and_remove_destination

    def replace_source_before_destination_cleanup(actual_record):
        atomic_write_text(source, "operator-replacement\n")
        return real_quarantine_destination(actual_record)

    monkeypatch.setattr(
        abandoned,
        "_quarantine_and_remove_destination",
        replace_source_before_destination_cleanup,
    )

    with pytest.raises(TransactionRecoveryError, match="changed|replaced|resolve manually"):
        abandoned.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert source.read_text() == "operator-replacement\n"
    preserved = [
        path
        for path in (destination, record.destination_quarantine)
        if path.exists() and path.read_text() == "handoff-original\n"
    ]
    assert preserved, "source replacement caused deletion of the last handoff copy"
    assert abandoned._journal_path.exists()


def test_quarantine_rename_failure_preserves_current_file(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "new.md"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)
    real_rename = transaction_mod._rename_noreplace

    def fail_quarantine(source, destination):
        if Path(source) == card and Path(destination).name.startswith(".mdq-"):
            raise OSError(errno.EIO, "quarantine rename failed")
        return real_rename(source, destination)

    monkeypatch.setattr(transaction_mod, "_rename_noreplace", fail_quarantine)

    with pytest.raises(TransactionRecoveryError, match="quarantine rename failed"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "transaction-created\n"
    assert transaction._journal_path.exists()


def test_quarantine_collision_preserves_both_paths_and_journal(
    memory_dir, handoffs_dir
):
    card = memory_dir / "new.md"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)
    quarantine = transaction._files[card].quarantine
    quarantine.write_text("operator-collision\n")

    with pytest.raises(TransactionRecoveryError, match="quarantine|matches"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "transaction-created\n"
    assert quarantine.read_text() == "operator-collision\n"
    assert transaction._journal_path.exists()


def test_unsupported_no_clobber_rename_fails_closed(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "new.md"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    transaction.after_write(card)

    def unsupported(source: Path, destination: Path):
        raise OSError(errno.ENOTSUP, "atomic rename unsupported")

    monkeypatch.setattr(transaction_mod, "_rename_noreplace", unsupported)

    with pytest.raises(TransactionRecoveryError, match="unsupported"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert card.read_text() == "transaction-created\n"
    assert transaction._journal_path.exists()


def test_new_file_retry_after_quarantine_unlink_preserves_replacement(
    memory_dir, handoffs_dir
):
    card = memory_dir / "new.md"
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    abandoned.after_write(card)
    record = abandoned._files[card]
    _rename_noreplace(card, record.quarantine)
    record.state = "quarantined"
    abandoned._write_journal()
    record.quarantine.unlink()
    atomic_write_text(card, "operator-replacement\n")
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "operator-replacement\n"
        recovered.commit()

    with ApplyTransaction(memory_dir, handoffs_dir) as retried:
        assert retried.recovered is False
        assert card.read_text() == "operator-replacement\n"
        retried.commit()


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX mode semantics")
def test_existing_file_rollback_restores_original_mode(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    card.chmod(0o640)

    with pytest.raises(RuntimeError, match="simulated failure"):
        with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
            transaction.before_write(card)
            atomic_write_text(card, "transaction-created\n")
            transaction.after_write(card)
            raise RuntimeError("simulated failure")

    assert card.read_text() == "original\n"
    assert stat.S_IMODE(card.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only mode semantics")
def test_existing_windows_read_only_file_is_refused_without_mutation(
    memory_dir, handoffs_dir
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    card.chmod(stat.S_IREAD)
    try:
        with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
            with pytest.raises(TransactionError, match="read-only"):
                transaction.write_text(card, "transaction-created\n")
            assert not transaction._journal_path.exists()
            transaction.commit()

        assert card.read_text() == "original\n"
        assert card.stat().st_mode & stat.S_IWRITE == 0
    finally:
        card.chmod(stat.S_IWRITE)


def test_before_write_rejects_dangling_symlink(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.symlink_to(memory_dir / "missing.md")

    with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
        with pytest.raises(TransactionError, match="not a regular file"):
            transaction.before_write(card)
        transaction.commit()

    assert card.is_symlink()


@pytest.mark.parametrize(
    ("first_name", "alias_name"),
    [
        ("Foo.md", "foo.md"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}.md", "cafe\N{COMBINING ACUTE ACCENT}.md"),
    ],
)
def test_runtime_rejects_normalized_visible_path_aliases(
    memory_dir, handoffs_dir, first_name, alias_name
):
    first = memory_dir / first_name
    first.write_text("original\n")

    with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
        transaction.before_write(first)
        with pytest.raises(TransactionRecoveryError, match="alias"):
            transaction.before_write(memory_dir / alias_name)

    assert first.read_text() == "original\n"


@pytest.mark.parametrize(
    ("source_name", "destination_name"),
    [
        ("Foo.md", "foo.md"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}.md", "cafe\N{COMBINING ACUTE ACCENT}.md"),
    ],
)
def test_before_move_rejects_source_destination_alias_before_journal(
    memory_dir, handoffs_dir, source_name, destination_name
):
    source = handoffs_dir / source_name
    source.write_text("handoff\n")

    with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
        with pytest.raises(TransactionRecoveryError, match="may alias"):
            transaction.before_move(source, handoffs_dir / destination_name)
        assert not transaction._journal_path.exists()
        transaction.commit()

    assert source.read_text() == "handoff\n"
    assert sum(entry.is_file() for entry in handoffs_dir.iterdir()) == 1


def test_before_move_revalidates_existing_source_plan(memory_dir, handoffs_dir):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("original handoff\n")

    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        transaction.before_move(source, destination)
        replacement = handoffs_dir / "replacement.tmp"
        replacement.write_text("operator replacement\n")
        replacement.replace(source)

        with pytest.raises(TransactionRecoveryError, match=r"changed after.*planned"):
            transaction.before_move(source, destination)

        assert not destination.exists()
    finally:
        transaction._release_lock()


def test_recovery_rolls_back_entire_batch_after_late_move_validation_failure(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    sources = [handoffs_dir / "one.md", handoffs_dir / "two.md"]
    for source in sources:
        source.write_text(f"{source.stem}\n")

    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    atomic_write_text(card, "transaction-created\n")
    abandoned.after_write(card)
    for source in sources:
        abandoned.move_handoff(
            source, handoffs_dir / "processed" / source.name
        )

    real_validate = abandoned._validate_move_for_commit
    validated = 0

    def fail_second_validation(record):
        nonlocal validated
        validated += 1
        if validated == 2:
            raise OSError(errno.EIO, "simulated validation crash")
        real_validate(record)

    monkeypatch.setattr(
        abandoned, "_validate_move_for_commit", fail_second_validation
    )

    with pytest.raises(OSError, match="simulated validation crash"):
        abandoned.commit()
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "original\n"
        for source in sources:
            assert source.read_text() == f"{source.stem}\n"
            assert not (handoffs_dir / "processed" / source.name).exists()
        recovered.commit()


def test_write_text_journals_future_identity_before_atomic_replace(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    real_replace = transaction_mod._rename_noreplace

    def crash_after_replace(source, destination):
        real_replace(source, destination)
        if Path(destination) == card:
            raise RuntimeError("simulated crash after replace")

    monkeypatch.setattr(
        transaction_mod,
        "_rename_noreplace",
        crash_after_replace,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        abandoned.write_text(card, "transaction-created\n")
    assert abandoned._files[card].pending_artifact is not None
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "original\n"
        recovered.commit()


def test_write_text_rejects_replaced_journaled_temporary(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n", encoding="utf-8")
    real_atomic_write = transaction_mod.atomic_write_text

    def replace_temporary_before_replacement_journal(path, content, **kwargs):
        if path != card:
            return real_atomic_write(path, content, **kwargs)
        real_before_replace = kwargs["before_replace"]

        def replace_temporary(temporary: Path) -> None:
            replacement = memory_dir / "unrelated.tmp"
            replacement.write_text("unrelated\n", encoding="utf-8")
            replacement.replace(temporary)
            real_before_replace(temporary)

        kwargs["before_replace"] = replace_temporary
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(
        transaction_mod,
        "atomic_write_text",
        replace_temporary_before_replacement_journal,
    )

    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        with pytest.raises(TransactionRecoveryError, match="temporary.*changed"):
            transaction.write_text(card, "transaction-created\n")
        assert transaction._files[card].pending_artifact is None
    finally:
        transaction._release_lock()

    assert card.read_text(encoding="utf-8") == "original\n"


def test_write_text_rejects_in_place_tampered_journaled_temporary(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n", encoding="utf-8")
    real_atomic_write = transaction_mod.atomic_write_text

    def tamper_before_replacement_journal(path, content, **kwargs):
        if path != card:
            return real_atomic_write(path, content, **kwargs)
        real_before_replace = kwargs["before_replace"]

        def tamper_temporary(temporary: Path) -> None:
            temporary.write_text("tampered in place\n", encoding="utf-8")
            real_before_replace(temporary)

        kwargs["before_replace"] = tamper_temporary
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(
        transaction_mod,
        "atomic_write_text",
        tamper_before_replacement_journal,
    )

    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    try:
        with pytest.raises(TransactionRecoveryError, match="unexpected content"):
            transaction.write_text(card, "transaction-created\n")
        assert transaction._files[card].pending_artifact is None
    finally:
        transaction._release_lock()

    assert card.read_text(encoding="utf-8") == "original\n"


def test_write_text_crash_before_replace_cleans_journaled_atomic_temp(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    class SimulatedCrash(BaseException):
        pass

    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    real_replace = transaction_mod._rename_noreplace

    def crash_before_replace(source, destination):
        if Path(destination) == card:
            raise SimulatedCrash("before replace")
        return real_replace(source, destination)

    monkeypatch.setattr(
        transaction_mod,
        "_rename_noreplace",
        crash_before_replace,
    )

    with pytest.raises(SimulatedCrash):
        abandoned.write_text(card, "transaction-created\n")
    record = abandoned._files[card]
    assert record.write_temp is not None
    assert record.write_temp.read_text() == "transaction-created\n"
    abandoned._release_lock()
    monkeypatch.setattr(transaction_mod, "_rename_noreplace", real_replace)

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "original\n"
        assert not record.write_temp.exists()
        recovered.commit()


def test_write_text_journals_temp_before_managed_content_is_written(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    class SimulatedCrash(BaseException):
        pass

    card = memory_dir / "card.md"
    card.write_text("original\n")
    leaked_temps: list[Path] = []
    real_atomic_write = transaction_mod.atomic_write_text

    def crash_after_content_before_replacement_journal(path, content, **kwargs):
        if path != card:
            return real_atomic_write(path, content, **kwargs)

        def crash_before_replacement_journal(temporary: Path) -> None:
            leaked_temps.append(temporary)
            assert temporary.read_text() == content
            raise SimulatedCrash("content durable before replacement journal")

        forwarded = {"before_replace": crash_before_replacement_journal}
        if "after_create" in kwargs:
            forwarded["after_create"] = kwargs["after_create"]
        return real_atomic_write(path, content, **forwarded)

    monkeypatch.setattr(
        transaction_mod, "atomic_write_text", crash_after_content_before_replacement_journal
    )
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()

    with pytest.raises(SimulatedCrash):
        abandoned.write_text(card, "managed secret\n")
    abandoned._release_lock()

    assert leaked_temps
    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "original\n"
        assert all(not path.exists() for path in leaked_temps)
        recovered.commit()


def test_recovery_uses_journal_handoffs_root_then_restores_callers_root(
    memory_dir, tmp_path
):
    original_handoffs = tmp_path / "handoffs-original"
    caller_handoffs = tmp_path / "handoffs-caller"
    original_handoffs.mkdir()
    caller_handoffs.mkdir()
    card = memory_dir / "card.md"
    card.write_text("original\n")

    abandoned = ApplyTransaction(memory_dir, original_handoffs)
    abandoned.__enter__()
    abandoned.before_write(card)
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, caller_handoffs) as recovered:
        assert recovered.recovered is True
        assert recovered.handoffs_dir == caller_handoffs.resolve()
        assert card.read_text() == "original\n"
        recovered.commit()


def test_compact_transaction_drops_recovered_ingest_handoffs_root(
    memory_dir, handoffs_dir
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    abandoned._release_lock()

    with ApplyTransaction(memory_dir) as recovered:
        assert recovered.recovered is True
        assert recovered.handoffs_dir is None
        assert recovered._handoffs_identity is None
        recovered.commit()


@pytest.mark.parametrize("boundary", ["before-replace", "after-replace"])
def test_second_write_crash_recovers_first_owned_artifact(
    memory_dir, handoffs_dir, monkeypatch, boundary
):
    from memory_doctor import transaction as transaction_mod

    class SimulatedCrash(BaseException):
        pass

    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.write_text(card, "first-transaction-version\n")
    real_replace = transaction_mod._rename_noreplace

    def crash_second_replace(source, destination):
        if Path(destination) == card:
            if boundary == "after-replace":
                real_replace(source, destination)
            raise SimulatedCrash(boundary)
        return real_replace(source, destination)

    monkeypatch.setattr(
        transaction_mod,
        "_rename_noreplace",
        crash_second_replace,
    )

    with pytest.raises(SimulatedCrash):
        abandoned.write_text(card, "second-transaction-version\n")
    abandoned._release_lock()
    monkeypatch.setattr(transaction_mod, "_rename_noreplace", real_replace)

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "original\n"
        recovered.commit()


def test_commit_refuses_file_replaced_after_managed_write(
    memory_dir, handoffs_dir
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "transaction-created\n")
    atomic_write_text(card, "operator-replacement\n")

    with pytest.raises(TransactionRecoveryError, match="changed before commit"):
        transaction.commit()

    assert transaction.committed is False
    assert card.read_text() == "operator-replacement\n"
    assert transaction._journal_path.exists()
    assert json.loads(transaction._journal_path.read_text())["phase"] == "active"
    with pytest.raises(TransactionRecoveryError, match="replacement"):
        transaction.__exit__(None, None, None)
    assert card.read_text() == "operator-replacement\n"


@pytest.mark.parametrize(
    ("residue", "message"),
    [
        ("quarantine", "rollback artifacts"),
        ("restore_temp", "rollback artifacts"),
        ("write_temp", "unfinished atomic write"),
    ],
)
def test_commit_refuses_managed_file_recovery_residue(
    memory_dir, handoffs_dir, residue, message
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "transaction-created\n")
    record = transaction._files[card]
    if residue == "write_temp":
        record.write_temp = card.parent / f".{card.name}.recovery.tmp"
        residue_path = record.write_temp
    else:
        residue_path = getattr(record, residue)
    residue_path.write_text("private-residue\n")
    transaction._write_journal()

    with pytest.raises(TransactionRecoveryError, match=message):
        transaction.commit()

    assert transaction.committed is False
    assert card.read_text() == "transaction-created\n"
    assert residue_path.read_text() == "private-residue\n"
    assert json.loads(transaction._journal_path.read_text())["phase"] == "active"
    transaction._release_lock()


@pytest.mark.parametrize("boundary", ["linked", "source-quarantined", "q-unlinked"])
def test_handoff_move_recovers_each_namespace_boundary_twice(
    memory_dir, handoffs_dir, boundary
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    record = abandoned._moves[0]
    os.link(source, destination)
    if boundary != "linked":
        record.state = "linked"
        abandoned._write_journal()
        _rename_noreplace(source, record.source_quarantine)
        if boundary == "q-unlinked":
            record.state = "moved"
            abandoned._write_journal()
            record.source_quarantine.unlink()
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert source.read_text() == "handoff\n"
        assert not destination.exists()
        recovered.commit()

    with ApplyTransaction(memory_dir, handoffs_dir) as retried:
        assert retried.recovered is False
        assert source.read_text() == "handoff\n"
        assert not destination.exists()
        retried.commit()


def test_handoff_hardlink_exdev_fails_before_source_deletion(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    real_link = transaction_mod.os.link

    def fail_move_link(actual_source, actual_destination, **kwargs):
        if (
            actual_source == source
            and actual_destination in (destination, destination.name)
        ):
            raise OSError(errno.EXDEV, "cross-device link")
        return real_link(actual_source, actual_destination, **kwargs)

    monkeypatch.setattr(transaction_mod.os, "link", fail_move_link)

    with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
        with pytest.raises(TransactionError, match="cross-device link"):
            transaction.move_handoff(source, destination)

    assert source.read_text() == "handoff\n"
    assert not destination.exists()


def test_eexist_race_never_relocates_unrelated_quarantine(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "new.md"
    holding = memory_dir / "held-transaction-artifact"
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "transaction-created\n")
    quarantine = transaction._files[card].quarantine
    real_rename = transaction_mod._rename_noreplace

    def inject_collision(source: Path, destination: Path):
        if source == card and destination == quarantine:
            source.rename(holding)
            destination.write_text("unrelated-collision\n")
            raise FileExistsError(errno.EEXIST, "raced collision")
        return real_rename(source, destination)

    monkeypatch.setattr(transaction_mod, "_rename_noreplace", inject_collision)

    with pytest.raises(TransactionRecoveryError, match="no longer matches"):
        transaction.__exit__(RuntimeError, RuntimeError("simulated failure"), None)

    assert not card.exists()
    assert holding.read_text() == "transaction-created\n"
    assert quarantine.read_text() == "unrelated-collision\n"
    assert transaction._journal_path.exists()


def test_destination_quarantine_keeps_last_copy_when_source_is_missing(
    memory_dir, handoffs_dir
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    record = abandoned._moves[0]
    source.rename(destination)
    abandoned.after_move(source, destination)
    _rename_noreplace(destination, record.destination_quarantine)
    abandoned._write_journal()
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert source.read_text() == "handoff\n"
        recovered.commit()

    assert source.read_text() == "handoff\n"
    assert not destination.exists()
    assert not record.destination_quarantine.exists()
    assert not abandoned._journal_path.exists()


def test_journal_rejects_private_path_colliding_with_managed_visible_path(
    memory_dir, handoffs_dir
):
    first = memory_dir / "card.md"
    visible_private_name = memory_dir / ".mdq-aaaaaaaaaaaa"
    first.write_text("first\n")
    visible_private_name.write_text("second\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(first)
    abandoned.before_write(visible_private_name)
    payload = json.loads(abandoned._journal_path.read_text())
    payload["files"][0]["quarantine"] = str(visible_private_name)
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="visible paths"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert first.read_text() == "first\n"
    assert visible_private_name.read_text() == "second\n"
    assert abandoned._journal_path.exists()


def test_journal_rejects_non_object_top_level(memory_dir, handoffs_dir):
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    atomic_write_text(abandoned._journal_path, "[]")
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="top level is not an object"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert abandoned._journal_path.read_text() == "[]"


def test_journal_rejects_move_with_identical_source_and_destination(
    memory_dir, handoffs_dir
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_move(source, destination)
    payload = json.loads(abandoned._journal_path.read_text())
    payload["moves"][0]["destination"] = str(source)
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="identical"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert source.read_text() == "handoff\n"
    assert abandoned._journal_path.exists()


def test_journal_rejects_original_size_mismatch(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    payload = json.loads(abandoned._journal_path.read_text())
    payload["files"][0]["original_identity"]["size"] += 1
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="original size does not match"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert card.read_text() == "original\n"
    assert abandoned._journal_path.exists()


def test_journal_rejects_zero_inode_identity(memory_dir, handoffs_dir):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    payload = json.loads(abandoned._journal_path.read_text())
    payload["files"][0]["original_identity"]["inode"] = 0
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="identity has invalid fields"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert card.read_text() == "original\n"
    assert abandoned._journal_path.exists()


@pytest.mark.parametrize(
    ("first_name", "alias_name"),
    [
        ("Foo.md", "foo.md"),
        ("café.md", "cafe\N{COMBINING ACUTE ACCENT}.md"),
    ],
)
def test_journal_rejects_normalized_file_path_aliases(
    memory_dir, handoffs_dir, first_name, alias_name
):
    card = memory_dir / first_name
    card.write_text("original\n", encoding="utf-8")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.before_write(card)
    payload = json.loads(abandoned._journal_path.read_text())
    alias = copy.deepcopy(payload["files"][0])
    alias["path"] = str(memory_dir / alias_name)
    alias["quarantine"] = str(memory_dir / ".mdq-111111111111")
    alias["restore_temp"] = str(memory_dir / ".mdr-222222222222")
    payload["files"].append(alias)
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="may alias"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert card.read_text() == "original\n"
    assert abandoned._journal_path.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path semantics")
def test_after_write_rejects_case_alias_callback(memory_dir, handoffs_dir):
    card = memory_dir / "Foo.md"
    card.write_text("original\n", encoding="utf-8")

    with ApplyTransaction(memory_dir, handoffs_dir) as transaction:
        transaction.before_write(card)
        atomic_write_text(card, "changed\n")
        with pytest.raises(TransactionError, match="without before_write"):
            transaction.after_write(memory_dir / "foo.md")
        transaction.after_write(card)

    assert card.read_text(encoding="utf-8") == "original\n"


def test_journal_rejects_overlapping_move_visible_paths(memory_dir, handoffs_dir):
    sources = [handoffs_dir / "one.md", handoffs_dir / "two.md"]
    destinations = [handoffs_dir / "processed" / source.name for source in sources]
    for source in sources:
        source.write_text(f"{source.stem}\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    for source, destination in zip(sources, destinations):
        abandoned.before_move(source, destination)
    payload = json.loads(abandoned._journal_path.read_text())
    payload["moves"][1]["source"] = payload["moves"][0]["destination"]
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="overlaps"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert sources[0].read_text() == "one\n"
    assert sources[1].read_text() == "two\n"
    assert abandoned._journal_path.exists()


@pytest.mark.parametrize("phase", ["committed", "rolled_back"])
def test_journal_rejects_phase_with_incomplete_file_state(
    memory_dir, handoffs_dir, phase
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.write_text(card, "transaction-created\n")
    payload = json.loads(abandoned._journal_path.read_text())
    payload["phase"] = phase
    payload["files"][0]["state"] = "done" if phase == "committed" else "planned"
    atomic_write_text(abandoned._journal_path, json.dumps(payload, sort_keys=True))
    abandoned._release_lock()

    with pytest.raises(TransactionRecoveryError, match="incomplete state"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert card.read_text() == "transaction-created\n"
    assert abandoned._journal_path.exists()


def test_committed_destination_mismatch_preserves_original_quarantine_and_journal(
    memory_dir, handoffs_dir, monkeypatch
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.move_handoff(source, destination)
    quarantine = transaction._moves[0].source_quarantine
    real_cleanup = transaction._cleanup_committed_journal

    def replace_after_commit_point():
        atomic_write_text(destination, "operator-replacement\n")
        real_cleanup()

    monkeypatch.setattr(
        transaction, "_cleanup_committed_journal", replace_after_commit_point
    )

    with pytest.raises(TransactionRecoveryError, match="commit succeeded"):
        transaction.commit()
    transaction.__exit__(None, None, None)

    assert transaction.committed is True
    assert destination.read_text() == "operator-replacement\n"
    assert quarantine.read_text() == "handoff\n"
    assert transaction._journal_path.exists()
    assert json.loads(transaction._journal_path.read_text())["phase"] == "committed"

    with pytest.raises(TransactionRecoveryError, match="commit succeeded"):
        ApplyTransaction(memory_dir, handoffs_dir).__enter__()

    assert destination.read_text() == "operator-replacement\n"
    assert quarantine.read_text() == "handoff\n"
    assert transaction._journal_path.exists()


def test_committed_journal_restart_cleans_quarantine_without_rollback(
    memory_dir, handoffs_dir, monkeypatch
):
    source = handoffs_dir / "pending.md"
    destination = handoffs_dir / "processed" / source.name
    source.write_text("handoff\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.move_handoff(source, destination)
    quarantine = abandoned._moves[0].source_quarantine

    def crash_before_cleanup():
        raise RuntimeError("simulated crash before committed cleanup")

    monkeypatch.setattr(
        abandoned, "_cleanup_committed_journal", crash_before_cleanup
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        abandoned.commit()
    assert abandoned.committed is True
    assert quarantine.read_text() == "handoff\n"
    assert json.loads(abandoned._journal_path.read_text())["phase"] == "committed"
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert not source.exists()
        assert destination.read_text() == "handoff\n"
        assert not quarantine.exists()
        recovered.commit()

    assert not abandoned._journal_path.exists()


def test_committed_cleanup_retries_after_first_of_multiple_quarantines(
    memory_dir, handoffs_dir, monkeypatch
):
    sources = [handoffs_dir / "one.md", handoffs_dir / "two.md"]
    for source in sources:
        source.write_text(f"{source.stem}\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    for source in sources:
        abandoned.move_handoff(
            source, handoffs_dir / "processed" / source.name
        )
    real_unlink = abandoned._unlink_private_owned
    cleaned = 0

    def crash_second_cleanup(path, expected, label):
        nonlocal cleaned
        cleaned += 1
        if cleaned == 2:
            raise OSError(errno.EIO, "simulated cleanup crash")
        real_unlink(path, expected, label)

    monkeypatch.setattr(abandoned, "_unlink_private_owned", crash_second_cleanup)

    with pytest.raises(TransactionRecoveryError, match="cleanup failed"):
        abandoned.commit()
    assert abandoned.committed is True
    abandoned._release_lock()

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        for source in sources:
            assert not source.exists()
            assert (handoffs_dir / "processed" / source.name).read_text() == (
                f"{source.stem}\n"
            )
        recovered.commit()

    assert not abandoned._journal_path.exists()


def test_rolled_back_journal_restart_finishes_cleanup_without_reapplying(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.write_text(card, "transaction-created\n")
    record = abandoned._files[card]

    def crash_before_cleanup():
        raise RuntimeError("simulated crash before rollback cleanup")

    monkeypatch.setattr(
        abandoned, "_cleanup_rolled_back_journal", crash_before_cleanup
    )

    with pytest.raises(RuntimeError, match="rollback cleanup"):
        abandoned.__exit__(RuntimeError, RuntimeError("apply failed"), None)

    assert card.read_text() == "original\n"
    assert record.quarantine.read_text() == "transaction-created\n"
    assert record.restore_temp.read_text() == "original\n"
    assert json.loads(abandoned._journal_path.read_text())["phase"] == "rolled_back"

    with ApplyTransaction(memory_dir, handoffs_dir) as recovered:
        assert recovered.recovered is True
        assert card.read_text() == "original\n"
        recovered.commit()

    assert not record.quarantine.exists()
    assert not record.restore_temp.exists()
    assert not abandoned._journal_path.exists()


def test_rolled_back_cleanup_preserves_private_original_after_visible_replacement(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "transaction-created\n")
    record = transaction._files[card]
    real_cleanup = transaction._cleanup_rolled_back_journal

    def replace_after_rollback_marker():
        assert json.loads(transaction._journal_path.read_text())["phase"] == (
            "rolled_back"
        )
        atomic_write_text(card, "operator-replacement\n")
        real_cleanup()

    monkeypatch.setattr(
        transaction, "_cleanup_rolled_back_journal", replace_after_rollback_marker
    )

    with pytest.raises(TransactionRecoveryError, match="changed|cleanup"):
        transaction.__exit__(RuntimeError, RuntimeError("apply failed"), None)

    assert card.read_text() == "operator-replacement\n"
    assert record.restore_temp.read_text() == "original\n"
    assert transaction._journal_path.exists()
    assert json.loads(transaction._journal_path.read_text())["phase"] == "rolled_back"


def test_committed_journal_clear_sync_failure_preserves_committed_files(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "card.md"
    card.write_text("original\n")
    transaction = ApplyTransaction(memory_dir, handoffs_dir)
    transaction.__enter__()
    transaction.write_text(card, "committed-change\n")
    real_sync = transaction_mod._fsync_directory
    failed = False

    def fail_first_clear_sync(path: Path):
        nonlocal failed
        if (
            path == transaction._state_dir
            and transaction._phase == "committed"
            and not transaction._journal_path.exists()
            and not failed
        ):
            failed = True
            raise OSError(errno.EIO, "state sync failed")
        return real_sync(path)

    monkeypatch.setattr(transaction_mod, "_fsync_directory", fail_first_clear_sync)

    with pytest.raises(TransactionRecoveryError, match="journal removal"):
        transaction.commit()

    assert transaction.committed is True
    transaction.__exit__(None, None, None)
    assert card.read_text() == "committed-change\n"

    with ApplyTransaction(memory_dir, handoffs_dir) as retried:
        assert card.read_text() == "committed-change\n"
        retried.commit()
