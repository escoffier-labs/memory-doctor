"""Exclusive apply lock and crash-recovery journal for multi-file mutations."""
from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from memory_doctor.safety import _fsync_directory, atomic_write_text

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None
    import msvcrt


class TransactionError(RuntimeError):
    """An apply transaction could not proceed safely."""


class TransactionRecoveryError(TransactionError):
    """An interrupted transaction could not be restored completely."""


def _native_no_replace() -> tuple[object, str]:
    """Return the native no-clobber rename function and ABI family."""
    if os.name == "nt":
        return os.rename, "windows"
    if os.name != "posix":
        raise OSError(errno.ENOTSUP, "atomic no-clobber rename is unsupported")

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":  # pragma: no cover - exercised on macOS
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOTSUP, "atomic no-clobber rename is unsupported")
        return renamex_np, "darwin"
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic no-clobber rename is unsupported")
    return renameat2, "linux"


def preflight_transaction_capabilities(
    memory_dir: Path, handoffs_dir: Path | None = None
) -> None:
    """Fail before mutation when required crash-safe primitives are unavailable."""
    if handoffs_dir is not None:
        try:
            memory_root = memory_dir.resolve()
            handoffs_root = handoffs_dir.resolve()
            memory_stat = memory_root.stat()
            handoffs_stat = handoffs_root.stat()
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot validate transaction roots: {exc}"
            ) from exc
        same_root = (memory_stat.st_dev, memory_stat.st_ino) == (
            handoffs_stat.st_dev,
            handoffs_stat.st_ino,
        )
        if same_root:
            raise TransactionRecoveryError(
                "memory and handoffs roots overlap; refusing before mutation"
            )
    try:
        _native_no_replace()
    except OSError as exc:
        raise TransactionRecoveryError(
            f"transaction recovery is unsupported on this platform: {exc}"
        ) from exc
    if not callable(getattr(os, "link", None)):
        raise TransactionRecoveryError(
            "transaction recovery requires no-clobber hard-link support"
        )


def preflight_managed_artifact(
    path: Path, *, label: str, required: bool = False
) -> None:
    """Reject file shapes the ownership journal cannot model, without writes."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if required:
            raise TransactionRecoveryError(f"{label} is missing: {path}") from None
        return
    except OSError as exc:
        raise TransactionRecoveryError(f"cannot inspect {label} {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    if not stat.S_ISREG(path_stat.st_mode) or bool(attributes & reparse_flag):
        raise TransactionRecoveryError(f"{label} is not a trusted regular file: {path}")
    read_only_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
    if os.name == "nt" and bool(attributes & read_only_flag):
        raise TransactionRecoveryError(
            f"{label} is read-only and cannot be changed safely on Windows: {path}"
        )
    if path_stat.st_ino == 0:
        raise TransactionRecoveryError(
            f"{label} filesystem does not provide a stable nonzero inode: {path}"
        )


def _state_key_for_resolved_path(path: Path) -> str:
    canonical_path = _visible_path_alias_key(path)
    return hashlib.sha256(os.fsencode(canonical_path)).hexdigest()


def has_pending_transaction_recovery(memory_dir: Path) -> bool:
    """Return whether an existing private journal needs locked recovery.

    This probe is intentionally read-only so apply callers can retain their
    state-free no-work fast path when no interrupted transaction exists.
    """
    try:
        memory_root = memory_dir.resolve()
        key = _state_key_for_resolved_path(memory_root)
    except (OSError, RuntimeError) as exc:
        raise TransactionRecoveryError(
            f"cannot resolve transaction root for recovery: {exc}"
        ) from exc
    state_dir = memory_root.parent / f".memory-doctor-{key}"
    try:
        state_stat = state_dir.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TransactionRecoveryError(
            f"cannot inspect transaction state for recovery: {exc}"
        ) from exc

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    state_attributes = getattr(state_stat, "st_file_attributes", 0)
    if not stat.S_ISDIR(state_stat.st_mode) or bool(state_attributes & reparse_flag):
        raise TransactionRecoveryError(
            f"transaction state path is not a trusted local directory: {state_dir}"
        )

    journal_path = state_dir / "apply.journal.json"
    try:
        journal_stat = journal_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TransactionRecoveryError(
            f"cannot inspect transaction journal for recovery: {exc}"
        ) from exc
    journal_attributes = getattr(journal_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(journal_stat.st_mode)
        or bool(journal_attributes & reparse_flag)
        or journal_stat.st_ino == 0
    ):
        raise TransactionRecoveryError(
            f"transaction journal is not a trusted local file: {journal_path}"
        )

    try:
        root_stat = memory_root.stat()
    except OSError as exc:
        raise TransactionRecoveryError(
            f"cannot inspect transaction root for recovery: {exc}"
        ) from exc
    if root_stat.st_ino == 0:
        raise TransactionRecoveryError(
            "cannot inspect transaction root for recovery: filesystem does not "
            "provide a stable nonzero inode"
        )
    return True


def _visible_path_spelling(path: Path) -> str:
    """Return an absolute spelling while preserving the final component."""
    if isinstance(path, Path):
        absolute = path if path.is_absolute() else Path.cwd() / path
        return str(absolute.parent.resolve(strict=False) / absolute.name)
    return os.path.abspath(os.fspath(path))


def _visible_path_alias_key(path: Path) -> str:
    """Conservatively identify path spellings that may alias one entry."""
    return unicodedata.normalize("NFC", _visible_path_spelling(path)).casefold()


def _same_visible_path_spelling(first: Path, second: Path) -> bool:
    """Compare normalized path spellings without platform Path equality rules."""
    return _visible_path_spelling(first) == _visible_path_spelling(second)


def preflight_visible_path_aliases(
    paths: list[Path], *, label: str, check_existing_identities: bool = True
) -> None:
    """Reject distinct planned names that may address one visible artifact."""
    seen_names: dict[str, tuple[Path, str]] = {}
    seen_identities: dict[tuple[int, int], tuple[Path, str]] = {}
    for path in paths:
        resolved = path.resolve(strict=False)
        spelling = _visible_path_spelling(path)
        key = _visible_path_alias_key(path)
        previous = seen_names.get(key)
        if previous is not None and previous[1] != spelling:
            raise TransactionRecoveryError(
                f"{label} paths may alias one filesystem entry: "
                f"{previous[0].name}, {resolved.name}"
            )
        seen_names[key] = (resolved, spelling)
        if not check_existing_identities:
            continue
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot inspect {label} path {path}: {exc}"
            ) from exc
        if path_stat.st_ino == 0:
            raise TransactionRecoveryError(
                f"{label} filesystem does not provide a stable nonzero inode: {path}"
            )
        identity = (path_stat.st_dev, path_stat.st_ino)
        previous = seen_identities.get(identity)
        if previous is not None and previous[1] != spelling:
            raise TransactionRecoveryError(
                f"{label} paths address one filesystem entry: "
                f"{previous[0].name}, {resolved.name}"
            )
        seen_identities[identity] = (resolved, spelling)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing an existing destination."""
    native, family = _native_no_replace()
    if family == "windows":  # pragma: no cover - exercised on Windows
        native(source, destination)
        return
    if family == "darwin":  # pragma: no cover - exercised on macOS
        native.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        native.restype = ctypes.c_int
        result = native(os.fsencode(source), os.fsencode(destination), 0x00000004)
    else:
        native.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        native.restype = ctypes.c_int
        result = native(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise OSError(error, "atomic no-clobber rename is unsupported")
    raise OSError(
        error,
        os.strerror(error),
        str(source),
        None,
        str(destination),
    )


@dataclass(frozen=True)
class _ArtifactIdentity:
    device: int
    inode: int
    size: int
    digest: str


@dataclass
class _FileRecord:
    path: Path
    existed: bool
    original: bytes | None
    original_identity: _ArtifactIdentity | None
    original_mode: int | None
    artifact: _ArtifactIdentity | None
    pending_artifact: _ArtifactIdentity | None
    quarantine: Path
    restore_temp: Path
    write_temp: Path | None = None
    write_temp_identity: _ArtifactIdentity | None = None
    publish_previous: _ArtifactIdentity | None = None
    state: str = "planned"


@dataclass
class _MoveRecord:
    source: Path
    destination: Path
    artifact: _ArtifactIdentity
    source_quarantine: Path
    destination_quarantine: Path
    state: str = "planned"


_EXPECTED_IDENTITY_UNSET = object()


class ApplyTransaction:
    """Serialize one memory directory and journal originals before mutation.

    The generated sibling state directory is a private, same-user namespace.
    Relocating or relinking it while an apply is active is unsupported.
    """

    def __init__(self, memory_dir: Path, handoffs_dir: Path | None = None):
        try:
            self.memory_dir = memory_dir.resolve()
            self.handoffs_dir = handoffs_dir.resolve() if handoffs_dir else None
            root_stat = self.memory_dir.stat()
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot initialize transaction roots: {exc}"
            ) from exc
        if root_stat.st_ino == 0:
            raise TransactionRecoveryError(
                "cannot initialize transaction root: filesystem does not provide "
                "a stable nonzero inode"
            )
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        if self.handoffs_dir is not None:
            try:
                handoffs_stat = self.handoffs_dir.stat()
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot initialize handoffs transaction root: {exc}"
                ) from exc
            if handoffs_stat.st_ino == 0:
                raise TransactionRecoveryError(
                    "cannot initialize handoffs transaction root: filesystem does "
                    "not provide a stable nonzero inode"
                )
            self._handoffs_identity = (
                handoffs_stat.st_dev,
                handoffs_stat.st_ino,
            )
        else:
            self._handoffs_identity = None
        self._requested_handoffs_dir = self.handoffs_dir
        self._requested_handoffs_identity = self._handoffs_identity
        key = _state_key_for_resolved_path(self.memory_dir)
        state_dir = self.memory_dir.parent / f".memory-doctor-{key}"
        self._state_dir = state_dir
        self._lock_path = state_dir / "apply.lock"
        self._journal_path = state_dir / "apply.journal.json"
        self._lock_file = None
        self._lock_acquired = False
        self._files: dict[Path, _FileRecord] = {}
        self._moves: list[_MoveRecord] = []
        self._watched_files: dict[Path, _ArtifactIdentity] = {}
        self._phase = "active"
        self._committed = False
        self._capabilities_checked = False
        self.recovered = False

    @property
    def committed(self) -> bool:
        return self._committed

    def __enter__(self) -> ApplyTransaction:
        try:
            created_state_dir = False
            try:
                self._state_dir.mkdir(mode=0o700)
                created_state_dir = True
            except FileExistsError:
                state_stat = self._state_dir.lstat()
                if not stat.S_ISDIR(state_stat.st_mode) or self._is_reparse_point(
                    state_stat
                ):
                    raise TransactionRecoveryError(
                        "transaction state path is not a trusted local directory: "
                        f"{self._state_dir}"
                    ) from None
            os.chmod(self._state_dir, 0o700)
            if created_state_dir:
                _fsync_directory(self._state_dir.parent)
            lock_flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                lock_flags |= os.O_NOFOLLOW
            self._validate_private_child(
                self._lock_path, allow_missing=True, label="lock"
            )
            fd = os.open(self._lock_path, lock_flags, 0o600)
            try:
                self._lock_file = os.fdopen(fd, "a+b")
            except Exception:
                os.close(fd)
                raise
        except TransactionRecoveryError:
            raise
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot prepare transaction state: {exc}"
            ) from exc
        try:
            self._validate_open_lock()
            self._acquire_lock()
            self._lock_acquired = True
            self._validate_current_roots()
            if self._lexists(self._journal_path):
                self._validate_private_child(
                    self._journal_path, allow_missing=False, label="journal"
                )
                self._load_journal()
                self._validate_current_roots()
                if self._phase == "committed":
                    self._cleanup_committed_journal()
                elif self._phase == "rolled_back":
                    self._cleanup_rolled_back_journal()
                else:
                    self._rollback(recovering=True)
                self.handoffs_dir = self._requested_handoffs_dir
                self._handoffs_identity = self._requested_handoffs_identity
                self.recovered = True
        except Exception:
            try:
                self._release_lock()
            except Exception:
                # Preserve the acquisition/recovery failure. Closing the file
                # handle releases any byte-range lock that might have landed.
                pass
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if not self._committed:
                self._rollback(recovering=False)
        finally:
            self._release_lock()

    def memory_file_identity(self, path: Path) -> _ArtifactIdentity | None:
        """Return the exact current identity of one managed memory path."""
        self._validate_current_roots()
        path = self._inside(path, self.memory_dir, "memory file")
        return self._identity_if_present(path, f"memory file {path.name}")

    def handoff_identity(self, path: Path) -> _ArtifactIdentity | None:
        """Return the exact current identity of one managed handoff path."""
        self._validate_current_roots()
        if self.handoffs_dir is None:
            raise TransactionError(
                "handoff identity requested without a handoffs directory"
            )
        path = self._inside(path, self.handoffs_dir, "handoff")
        return self._identity_if_present(path, f"handoff {path.name}")

    def watch_memory_file(
        self, path: Path, expected_identity: _ArtifactIdentity
    ) -> None:
        """Require a read dependency to remain unchanged through commit."""
        self._validate_current_roots()
        path = self._inside(path, self.memory_dir, "watched memory file")
        actual = self._identity_if_present(path, f"memory file {path.name}")
        if actual != expected_identity:
            raise TransactionRecoveryError(
                f"memory file {path.name} changed before it could be watched"
            )
        previous = self._watched_files.get(path)
        if previous is not None and previous != expected_identity:
            raise TransactionRecoveryError(
                f"memory file {path.name} changed between transaction watches"
            )
        self._watched_files[path] = expected_identity

    def before_write(
        self,
        path: Path,
        *,
        expected_identity=_EXPECTED_IDENTITY_UNSET,
    ) -> None:
        """Persist a file's exact original state before its first write."""
        self.preflight_mutations()
        self._validate_current_roots()
        path = self._inside(path, self.memory_dir, "memory file")
        preflight_visible_path_aliases(
            [*self._files, path],
            label="managed memory",
            check_existing_identities=False,
        )
        if path in self._files:
            if (
                expected_identity is not _EXPECTED_IDENTITY_UNSET
                and self._identity_if_present(path, f"memory file {path.name}")
                != expected_identity
            ):
                raise TransactionRecoveryError(
                    f"memory file {path.name} changed after it was read; "
                    "preserve the replacement and retry"
                )
            return
        self._ensure_visible_path_available(path)
        if self._lexists(path):
            try:
                original, identity, mode = self._snapshot_regular(path)
            except OSError as exc:
                raise TransactionError(
                    f"cannot record original file {path.name}: {exc}"
                ) from exc
            record = _FileRecord(
                path=path,
                existed=True,
                original=original,
                original_identity=identity,
                original_mode=mode,
                artifact=None,
                pending_artifact=None,
                quarantine=self._allocate_quarantine(path, ".mdq-"),
                restore_temp=self._allocate_quarantine(path, ".mdr-"),
            )
        else:
            record = _FileRecord(
                path=path,
                existed=False,
                original=None,
                original_identity=None,
                original_mode=None,
                artifact=None,
                pending_artifact=None,
                quarantine=self._allocate_quarantine(path, ".mdq-"),
                restore_temp=self._allocate_quarantine(path, ".mdr-"),
            )
        if (
            expected_identity is not _EXPECTED_IDENTITY_UNSET
            and record.original_identity != expected_identity
        ):
            raise TransactionRecoveryError(
                f"memory file {path.name} changed after it was read; "
                "preserve the replacement and retry"
            )
        self._files[path] = record
        self._write_journal()

    def before_move(
        self,
        source: Path,
        destination: Path,
        *,
        expected_identity=_EXPECTED_IDENTITY_UNSET,
    ) -> None:
        """Persist a planned handoff move before changing either path."""
        self.preflight_mutations()
        self._validate_current_roots()
        if self.handoffs_dir is None:
            raise TransactionError("handoff move requested without a handoffs directory")
        source = self._inside(source, self.handoffs_dir, "handoff source")
        destination = self._inside(destination, self.handoffs_dir, "handoff destination")
        if any(
            _same_visible_path_spelling(record.source, source)
            and _same_visible_path_spelling(record.destination, destination)
            for record in self._moves
        ):
            return
        preflight_visible_path_aliases(
            [source, destination], label="handoff move"
        )
        if _same_visible_path_spelling(source, destination):
            raise TransactionRecoveryError(
                f"handoff move source and destination are identical: {source.name}"
            )
        self._ensure_visible_path_available(source)
        self._ensure_visible_path_available(destination)
        if self._lexists(destination):
            raise TransactionError(f"processed handoff already exists: {destination.name}")
        try:
            artifact = self._artifact_identity(source)
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot record handoff artifact {source.name}: {exc}"
            ) from exc
        if (
            expected_identity is not _EXPECTED_IDENTITY_UNSET
            and artifact != expected_identity
        ):
            raise TransactionRecoveryError(
                f"handoff {source.name} changed after it was parsed; "
                "preserve the replacement and retry"
            )
        self._moves.append(
            _MoveRecord(
                source=source,
                destination=destination,
                artifact=artifact,
                source_quarantine=self._allocate_quarantine(source, ".mds-"),
                destination_quarantine=self._allocate_quarantine(
                    destination, ".mdd-"
                ),
            )
        )
        self._write_journal()

    def preflight_mutations(self) -> None:
        """Prove recovery primitives on the mutation filesystem under the lock."""
        if self._capabilities_checked:
            return
        if self._lock_file is None:
            raise TransactionRecoveryError(
                "transaction mutation preflight requires the apply lock"
            )
        preflight_transaction_capabilities(self.memory_dir, self.handoffs_dir)
        probe_directories = [self._state_dir, self.memory_dir]
        if self.handoffs_dir is not None:
            probe_directories.append(self.handoffs_dir)
            processed = self.handoffs_dir / "processed"
            try:
                processed_stat = processed.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot inspect processed handoff directory: {exc}"
                ) from exc
            else:
                if (
                    not stat.S_ISDIR(processed_stat.st_mode)
                    or self._is_reparse_point(processed_stat)
                ):
                    raise TransactionRecoveryError(
                        "processed handoff path is not a trusted local directory: "
                        f"{processed}"
                    )
                try:
                    processed_root = processed.resolve(strict=True)
                    processed_root.relative_to(self.handoffs_dir)
                except (OSError, ValueError) as exc:
                    raise TransactionRecoveryError(
                        "processed handoff directory escapes the configured root: "
                        f"{processed}: {exc}"
                    ) from exc
                probe_directories.append(processed_root)

        seen_directories: set[tuple[int, int]] = set()
        for directory in probe_directories:
            try:
                directory_stat = directory.lstat()
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot inspect transaction mutation directory {directory}: {exc}"
                ) from exc
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or self._is_reparse_point(directory_stat)
                or directory_stat.st_ino == 0
            ):
                raise TransactionRecoveryError(
                    "transaction mutation directory is not a trusted directory with "
                    f"a stable identity: {directory}"
                )
            identity = (directory_stat.st_dev, directory_stat.st_ino)
            if identity in seen_directories:
                continue
            seen_directories.add(identity)
            self._probe_mutation_directory(directory)
        self._capabilities_checked = True

    def _probe_mutation_directory(self, directory: Path) -> None:
        """Prove link and no-clobber rename inside one mutation directory."""
        token = uuid.uuid4().hex
        source = directory / f".memory-doctor-cap-{token}.source"
        linked = directory / f".memory-doctor-cap-{token}.linked"
        renamed = directory / f".memory-doctor-cap-{token}.renamed"
        created: list[Path] = []
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(source, flags, 0o600)
            created.append(source)
            try:
                os.write(fd, b"transaction capability probe\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(source, linked, follow_symlinks=False)
            created.append(linked)
            _rename_noreplace(source, renamed)
            created.remove(source)
            created.append(renamed)
            identities = {
                (path.stat().st_dev, path.stat().st_ino) for path in (linked, renamed)
            }
            if len(identities) != 1 or next(iter(identities))[1] == 0:
                raise OSError("hard-link capability probe changed artifact identity")
        except (NotImplementedError, OSError) as exc:
            raise TransactionRecoveryError(
                "transaction recovery primitives are unavailable in "
                f"{directory}: {exc}"
            ) from exc
        finally:
            cleanup_errors: list[str] = []
            for path in reversed(created):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    cleanup_errors.append(f"{path.name}: {exc}")
            try:
                _fsync_directory(directory)
            except OSError as exc:
                cleanup_errors.append(f"directory sync: {exc}")
            if cleanup_errors and sys.exc_info()[0] is None:
                raise TransactionRecoveryError(
                    "transaction capability probe cleanup failed: "
                    + "; ".join(cleanup_errors)
                )

    def after_move(self, source: Path, destination: Path) -> None:
        """Persist the exact artifact produced by a completed handoff move."""
        self._validate_current_roots()
        if self.handoffs_dir is None:
            raise TransactionError("handoff move completed without a handoffs directory")
        source = self._inside(source, self.handoffs_dir, "handoff source")
        destination = self._inside(
            destination, self.handoffs_dir, "handoff destination"
        )
        record = next(
            (
                item
                for item in self._moves
                if _same_visible_path_spelling(item.source, source)
                and _same_visible_path_spelling(item.destination, destination)
            ),
            None,
        )
        if record is None:
            raise TransactionError(
                f"handoff move completed without before_move: {source.name}"
            )
        try:
            moved = self._artifact_identity(destination)
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot record moved handoff artifact {destination.name}: {exc}"
            ) from exc
        if moved != record.artifact:
            raise TransactionRecoveryError(
                f"moved handoff {destination.name} no longer matches its source"
            )
        record.state = "moved"
        self._write_journal()

    def move_handoff(
        self,
        source: Path,
        destination: Path,
        *,
        expected_identity=_EXPECTED_IDENTITY_UNSET,
    ) -> None:
        """Move one handoff with a journaled, no-clobber hard-link protocol."""
        self.before_move(
            source,
            destination,
            expected_identity=expected_identity,
        )
        source = self._inside(source, self.handoffs_dir, "handoff source")
        destination = self._inside(
            destination, self.handoffs_dir, "handoff destination"
        )
        record = next(
            item
            for item in self._moves
            if item.source == source and item.destination == destination
        )
        if self._lexists(record.source_quarantine) or self._lexists(
            record.destination_quarantine
        ):
            raise TransactionRecoveryError(
                "handoff recovery path collision; preserve all paths and resolve manually"
            )
        try:
            os.link(source, destination, follow_symlinks=False)
        except NotImplementedError as exc:
            raise TransactionRecoveryError(
                "cannot move handoff safely: hard links are unsupported"
            ) from exc
        except OSError as exc:
            unsupported = {
                errno.EXDEV,
                getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL),
            }
            if exc.errno in unsupported:
                raise TransactionRecoveryError(
                    f"cannot move handoff safely on this filesystem: {exc}"
                ) from exc
            raise TransactionError(
                f"cannot link handoff into processed without clobbering: {exc}"
            ) from exc
        _fsync_directory(destination.parent)
        moved_identity = self._identity_if_present(
            destination, f"processed handoff {destination.name}"
        )
        if moved_identity != record.artifact:
            raise TransactionRecoveryError(
                "processed hard link does not match the recorded handoff; "
                "preserve both paths and resolve manually"
            )
        record.state = "linked"
        self._write_journal()
        quarantine = self._move_to_quarantine(
            source,
            record.source_quarantine,
            record.artifact,
            f"handoff source {source.name}",
        )
        if quarantine is None:
            raise TransactionRecoveryError(
                "handoff source disappeared before it could be quarantined"
            )
        record.state = "moved"
        self._write_journal()
        self.after_move(source, destination)

    def after_write(self, path: Path) -> None:
        """Record the exact artifact created by the most recent managed write."""
        self._validate_current_roots()
        path = self._inside(path, self.memory_dir, "memory file")
        record = next(
            (
                item
                for recorded_path, item in self._files.items()
                if _same_visible_path_spelling(recorded_path, path)
            ),
            None,
        )
        if record is None:
            raise TransactionError(f"write completed without before_write: {path.name}")
        try:
            record.artifact = self._artifact_identity(path)
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot record transaction-created artifact {path.name}: {exc}"
            ) from exc
        self._write_journal()

    def write_text(
        self,
        path: Path,
        content: str,
        *,
        expected_identity=_EXPECTED_IDENTITY_UNSET,
    ) -> None:
        """Atomically write text after journaling its exact future identity."""
        self.before_write(path, expected_identity=expected_identity)
        path = self._inside(path, self.memory_dir, "memory file")
        record = self._files[path]
        previous_visible = record.artifact
        if previous_visible is None and record.existed:
            previous_visible = record.original_identity

        def journal_temporary_name(temporary: Path) -> None:
            try:
                record.write_temp_identity = self._artifact_identity(temporary)
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot record atomic temp for {path.name}: {exc}"
                ) from exc
            record.write_temp = temporary
            self._write_journal()

        def journal_replacement(temporary: Path) -> None:
            try:
                record.pending_artifact = self._artifact_identity(temporary)
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot record future artifact for {path.name}: {exc}"
                ) from exc
            self._write_journal()

        def publish_replacement(temporary: Path, destination: Path) -> None:
            record.publish_previous = previous_visible
            self._write_journal()
            if previous_visible is not None:
                try:
                    quarantined = self._move_to_quarantine(
                        destination,
                        record.quarantine,
                        previous_visible,
                        f"memory file {destination.name}",
                    )
                except OSError as exc:
                    raise TransactionRecoveryError(
                        f"memory file {destination.name} changed after it was read; "
                        "preserve the replacement and retry"
                    ) from exc
                if quarantined is None:
                    raise TransactionRecoveryError(
                        f"memory file {destination.name} disappeared before it "
                        "could be replaced safely"
                    )
            try:
                _rename_noreplace(temporary, destination)
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot publish memory file {destination.name} without "
                    "clobbering a replacement; private recovery files retained"
                ) from exc
            _fsync_directory(destination.parent)

        atomic_write_text(
            path,
            content,
            after_create=journal_temporary_name,
            before_replace=journal_replacement,
            replace_file=publish_replacement,
            cleanup_temp_on_error=False,
        )
        actual = self._artifact_identity(path)
        if record.pending_artifact != actual:
            raise TransactionRecoveryError(
                f"written artifact {path.name} changed during atomic replacement"
            )
        record.artifact = actual
        if previous_visible is not None:
            self._unlink_private_owned(
                record.quarantine,
                previous_visible,
                f"previous memory file {record.quarantine.name}",
            )
        record.pending_artifact = None
        record.write_temp = None
        record.write_temp_identity = None
        record.publish_previous = None
        self._write_journal()

    def commit(self) -> None:
        """Discard recovery data after every planned mutation completed."""
        self._validate_current_roots()
        for record in self._moves:
            self._validate_move_for_commit(record)
        for record in self._files.values():
            self._validate_file_for_commit(record)
        for path, expected_identity in self._watched_files.items():
            if self._identity_if_present(
                path, f"watched memory file {path.name}"
            ) != expected_identity:
                raise TransactionRecoveryError(
                    f"watched memory file {path.name} changed before commit; "
                    "active recovery journal retained"
                )
        self._phase = "committed"
        try:
            self._write_journal()
        except OSError as exc:
            try:
                self._load_journal()
            except TransactionRecoveryError:
                on_disk_phase = None
            else:
                on_disk_phase = self._phase
            if on_disk_phase == "active":
                self._phase = "active"
                raise
            # A committed or invalid journal cannot prove the marker rename
            # failed. Never roll back a possibly committed batch.
            self._committed = True
            raise TransactionRecoveryError(
                "transaction commit status is indeterminate after journal "
                "durability failure; preserve files and recovery journal for "
                "manual inspection"
            ) from exc
        self._committed = True
        self._cleanup_committed_journal()

    def _cleanup_committed_journal(self) -> None:
        try:
            self._cleanup_committed_journal_impl()
        except TransactionRecoveryError:
            raise
        except OSError as exc:
            raise TransactionRecoveryError(
                "transaction commit succeeded but cleanup is incomplete; "
                f"recovery journal retained: {exc}"
            ) from exc

    def _cleanup_committed_journal_impl(self) -> None:
        if self._phase != "committed":
            raise TransactionRecoveryError(
                "refusing committed cleanup for an active transaction"
            )
        for record in self._files.values():
            self._validate_file_for_commit(record)
        for record in self._moves:
            if self._lexists(record.source):
                raise TransactionRecoveryError(
                    "transaction committed but a handoff source name exists; "
                    f"preserve {record.source.name} and resolve manually"
                )
            destination_identity = self._identity_if_present(
                record.destination,
                f"processed handoff {record.destination.name}",
            )
            if destination_identity != record.artifact:
                location = (
                    f"original preserved at {record.source_quarantine.name}"
                    if self._lexists(record.source_quarantine)
                    else "private original was already cleaned"
                )
                raise TransactionRecoveryError(
                    "transaction commit succeeded but the processed handoff changed; "
                    f"{location}, resolve manually"
                )
            try:
                self._unlink_private_owned(
                    record.source_quarantine,
                    record.artifact,
                    f"source quarantine {record.source_quarantine.name}",
                )
            except OSError as exc:
                raise TransactionRecoveryError(
                    "transaction commit succeeded but private handoff cleanup failed; "
                    f"resolve manually: {exc}"
                ) from exc
        self._clear_journal()
        self._files.clear()
        self._moves.clear()
        self._watched_files.clear()
        self._phase = "active"

    def _validate_move_for_commit(self, record: _MoveRecord) -> None:
        if record.state == "done":
            return
        if record.state != "moved":
            raise TransactionError(
                f"handoff move for {record.source.name} did not complete"
            )
        if self._lexists(record.source):
            raise TransactionRecoveryError(
                f"handoff source {record.source.name} unexpectedly exists"
            )
        if self._identity_if_present(
            record.destination, f"processed handoff {record.destination.name}"
        ) != record.artifact:
            raise TransactionRecoveryError(
                f"processed handoff {record.destination.name} changed before commit"
            )
        if self._identity_if_present(
            record.source_quarantine,
            f"source quarantine {record.source_quarantine.name}",
        ) != record.artifact:
            raise TransactionRecoveryError(
                f"handoff source quarantine {record.source_quarantine.name} "
                "changed before commit"
            )

    def _validate_file_for_commit(self, record: _FileRecord) -> None:
        if (
            record.state != "planned"
            or record.artifact is None
            or record.pending_artifact is not None
            or record.write_temp_identity is not None
            or record.publish_previous is not None
        ):
            raise TransactionRecoveryError(
                f"memory file {record.path.name} has incomplete transaction state"
            )
        if self._identity_if_present(
            record.path, f"memory file {record.path.name}"
        ) != record.artifact:
            raise TransactionRecoveryError(
                f"memory file {record.path.name} changed before commit; "
                "active recovery journal retained"
            )
        if self._lexists(record.quarantine) or self._lexists(record.restore_temp):
            raise TransactionRecoveryError(
                f"memory file {record.path.name} has rollback artifacts before commit"
            )
        if record.write_temp is not None:
            raise TransactionRecoveryError(
                f"memory file {record.path.name} has an unfinished atomic write"
            )

    def _inside(self, path: Path, root: Path, label: str) -> Path:
        absolute = path if path.is_absolute() else Path.cwd() / path
        candidate = absolute.parent.resolve(strict=False) / absolute.name
        try:
            candidate.relative_to(root)
        except ValueError:
            raise TransactionError(f"{label} escapes configured root: {path}") from None
        return candidate

    @staticmethod
    def _lexists(path: Path) -> bool:
        return os.path.lexists(path)

    @staticmethod
    def _is_reparse_point(path_stat: os.stat_result) -> bool:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(path_stat, "st_file_attributes", 0)
        return bool(attributes & reparse_flag)

    def _validate_private_child(
        self, path: Path, *, allow_missing: bool, label: str
    ) -> None:
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise TransactionRecoveryError(
                f"transaction {label} path disappeared: {path}"
            ) from None
        if not stat.S_ISREG(path_stat.st_mode) or self._is_reparse_point(path_stat):
            raise TransactionRecoveryError(
                f"transaction {label} path is not a trusted regular file: {path}"
            )

    def _validate_open_lock(self) -> None:
        if self._lock_file is None:
            raise TransactionRecoveryError("transaction lock is not open")
        try:
            path_stat = self._lock_path.lstat()
            open_stat = os.fstat(self._lock_file.fileno())
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot validate transaction lock: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or not stat.S_ISREG(open_stat.st_mode)
            or self._is_reparse_point(path_stat)
            or self._is_reparse_point(open_stat)
            or (path_stat.st_dev, path_stat.st_ino)
            != (open_stat.st_dev, open_stat.st_ino)
        ):
            raise TransactionRecoveryError(
                "transaction lock path changed or is not a trusted regular file"
            )

    def _allocate_quarantine(self, path: Path, prefix: str) -> Path:
        reserved = set(self._files)
        for record in self._files.values():
            reserved.update((record.quarantine, record.restore_temp))
            if record.write_temp is not None:
                reserved.add(record.write_temp)
        for record in self._moves:
            reserved.update(
                (
                    record.source,
                    record.destination,
                    record.source_quarantine,
                    record.destination_quarantine,
                )
            )
        reserved_keys = {_visible_path_alias_key(item) for item in reserved}
        for _ in range(32):
            quarantine = path.parent / f"{prefix}{uuid.uuid4().hex[:12]}"
            if (
                _visible_path_alias_key(quarantine) not in reserved_keys
                and not self._lexists(quarantine)
            ):
                return quarantine
        raise TransactionError(
            f"cannot allocate a private quarantine path for {path.name}"
        )

    def _ensure_visible_path_available(self, path: Path) -> None:
        private_paths: set[Path] = set()
        visible_paths = set(self._files)
        for record in self._files.values():
            private_paths.update((record.quarantine, record.restore_temp))
            if record.write_temp is not None:
                private_paths.add(record.write_temp)
        for record in self._moves:
            visible_paths.update((record.source, record.destination))
            private_paths.update(
                (record.source_quarantine, record.destination_quarantine)
            )
        path_key = _visible_path_alias_key(path)
        visible_alias = next(
            (
                other
                for other in visible_paths
                if _visible_path_alias_key(other) == path_key
            ),
            None,
        )
        if visible_alias is not None:
            raise TransactionRecoveryError(
                "managed visible path overlaps another transaction record: "
                f"{path.name} aliases {visible_alias.name}"
            )
        private_alias = next(
            (
                other
                for other in private_paths
                if _visible_path_alias_key(other) == path_key
            ),
            None,
        )
        if private_alias is not None:
            raise TransactionError(
                f"managed path collides with private recovery path: {path.name}"
            )
        if self._lexists(path):
            try:
                path_stat = path.lstat()
            except OSError as exc:
                raise TransactionRecoveryError(
                    f"cannot inspect managed visible path {path.name}: {exc}"
                ) from exc
            if path_stat.st_ino == 0:
                raise TransactionRecoveryError(
                    "managed visible path filesystem does not provide a stable "
                    f"nonzero inode: {path.name}"
                )
            identity = (path_stat.st_dev, path_stat.st_ino)
            for other in visible_paths:
                if not self._lexists(other):
                    continue
                try:
                    other_stat = other.lstat()
                except OSError as exc:
                    raise TransactionRecoveryError(
                        f"cannot inspect managed visible path {other.name}: {exc}"
                    ) from exc
                if (other_stat.st_dev, other_stat.st_ino) == identity:
                    raise TransactionRecoveryError(
                        "managed visible paths address one filesystem entry: "
                        f"{path.name}, {other.name}"
                    )

    @staticmethod
    def _valid_private_path(path: Path, parent: Path, prefix: str) -> bool:
        suffix = path.name.removeprefix(prefix)
        return (
            path.parent == parent
            and path.name.startswith(prefix)
            and len(suffix) == 12
            and all(character in "0123456789abcdef" for character in suffix)
        )

    def _snapshot_regular(
        self, path: Path
    ) -> tuple[bytes, _ArtifactIdentity, int]:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or self._is_reparse_point(before):
            raise OSError("transaction artifact is not a regular file")
        if self._is_windows_read_only(before):
            raise OSError(
                "read-only transaction artifacts are unsupported on Windows"
            )
        if before.st_ino == 0:
            raise OSError(
                "transaction artifact filesystem does not provide a stable "
                "nonzero inode"
            )
        content = path.read_bytes()
        after = path.lstat()
        if not stat.S_ISREG(after.st_mode) or self._is_reparse_point(after):
            raise OSError("transaction artifact is not a regular file")
        if self._is_windows_read_only(after):
            raise OSError(
                "read-only transaction artifacts are unsupported on Windows"
            )
        if after.st_ino == 0:
            raise OSError(
                "transaction artifact filesystem does not provide a stable "
                "nonzero inode"
            )
        before_marker = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            stat.S_IMODE(before.st_mode),
            getattr(before, "st_file_attributes", None),
            getattr(before, "st_reparse_tag", None),
        )
        after_marker = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            stat.S_IMODE(after.st_mode),
            getattr(after, "st_file_attributes", None),
            getattr(after, "st_reparse_tag", None),
        )
        if before_marker != after_marker:
            raise OSError("transaction artifact changed while it was inspected")
        identity = _ArtifactIdentity(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            digest=hashlib.sha256(content).hexdigest(),
        )
        return content, identity, stat.S_IMODE(after.st_mode)

    @staticmethod
    def _is_windows_read_only(path_stat: os.stat_result) -> bool:
        read_only_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        attributes = getattr(path_stat, "st_file_attributes", 0)
        return os.name == "nt" and bool(attributes & read_only_flag)

    @staticmethod
    def _current_root_identity(path: Path, label: str) -> tuple[int, int]:
        try:
            root_stat = path.stat()
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot validate {label} identity at {path}: {exc}"
            ) from exc
        if not stat.S_ISDIR(root_stat.st_mode):
            raise TransactionRecoveryError(f"{label} is not a directory: {path}")
        if root_stat.st_ino == 0:
            raise TransactionRecoveryError(
                f"{label} filesystem does not provide a stable nonzero inode: {path}"
            )
        return root_stat.st_dev, root_stat.st_ino

    def _validate_current_roots(self) -> None:
        if self._current_root_identity(self.memory_dir, "memory root") != self._root_identity:
            raise TransactionRecoveryError(
                "memory root identity changed; recovery requires manual inspection"
            )
        if self.handoffs_dir is not None and self._handoffs_identity is not None:
            if (
                self._current_root_identity(self.handoffs_dir, "handoffs root")
                != self._handoffs_identity
            ):
                raise TransactionRecoveryError(
                    "handoffs root identity changed; recovery requires manual inspection"
                )

    def _artifact_identity(self, path: Path) -> _ArtifactIdentity:
        return self._snapshot_regular(path)[1]

    def _write_journal(self) -> None:
        payload = {
            "version": 2,
            "phase": self._phase,
            "memory_dir": str(self.memory_dir),
            "memory_root": {
                "device": self._root_identity[0],
                "inode": self._root_identity[1],
            },
            "handoffs_dir": str(self.handoffs_dir) if self.handoffs_dir else None,
            "handoffs_root": (
                {
                    "device": self._handoffs_identity[0],
                    "inode": self._handoffs_identity[1],
                }
                if self._handoffs_identity is not None
                else None
            ),
            "files": [
                {
                    "path": str(record.path),
                    "existed": record.existed,
                    "original": (
                        base64.b64encode(record.original).decode("ascii")
                        if record.original is not None
                        else None
                    ),
                    "original_identity": self._identity_payload(
                        record.original_identity
                    ),
                    "original_mode": record.original_mode,
                    "artifact": self._identity_payload(record.artifact),
                    "pending_artifact": self._identity_payload(
                        record.pending_artifact
                    ),
                    "quarantine": str(record.quarantine),
                    "restore_temp": str(record.restore_temp),
                    "write_temp": (
                        str(record.write_temp)
                        if record.write_temp is not None
                        else None
                    ),
                    "write_temp_identity": self._identity_payload(
                        record.write_temp_identity
                    ),
                    "publish_previous": self._identity_payload(
                        record.publish_previous
                    ),
                    "state": record.state,
                }
                for record in self._files.values()
            ],
            "moves": [
                {
                    "source": str(record.source),
                    "destination": str(record.destination),
                    "artifact": self._identity_payload(record.artifact),
                    "source_quarantine": str(record.source_quarantine),
                    "destination_quarantine": str(
                        record.destination_quarantine
                    ),
                    "state": record.state,
                }
                for record in self._moves
            ],
        }
        atomic_write_text(self._journal_path, json.dumps(payload, sort_keys=True))

    @staticmethod
    def _identity_payload(identity: _ArtifactIdentity | None) -> dict | None:
        if identity is None:
            return None
        return {
            "device": identity.device,
            "inode": identity.inode,
            "size": identity.size,
            "digest": identity.digest,
        }

    @staticmethod
    def _identity_from_payload(payload, label: str) -> _ArtifactIdentity | None:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError(f"{label} identity is not an object")
        identity = _ArtifactIdentity(
            device=payload["device"],
            inode=payload["inode"],
            size=payload["size"],
            digest=payload["digest"],
        )
        numeric = (identity.device, identity.inode, identity.size)
        if (
            not all(isinstance(value, int) and not isinstance(value, bool) for value in numeric)
            or any(value < 0 for value in numeric)
            or identity.inode == 0
            or not isinstance(identity.digest, str)
            or len(identity.digest) != 64
            or any(character not in "0123456789abcdef" for character in identity.digest)
        ):
            raise ValueError(f"{label} identity has invalid fields")
        return identity

    def _load_journal(self) -> None:
        try:
            payload = json.loads(self._journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("journal top level is not an object")
            if payload.get("version") != 2:
                raise ValueError("unsupported journal version")
            phase = payload.get("phase")
            if phase not in {"active", "committed", "rolled_back"}:
                raise ValueError("journal phase is invalid")
            self._phase = phase
            if Path(payload["memory_dir"]).resolve() != self.memory_dir:
                raise ValueError("journal memory directory does not match")
            root_identity = payload["memory_root"]
            if not isinstance(root_identity, dict):
                raise ValueError("journal memory root identity is invalid")
            journal_root_identity = (
                root_identity["device"],
                root_identity["inode"],
            )
            if not all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in journal_root_identity
            ) or journal_root_identity[1] == 0:
                raise ValueError("journal memory root identity is invalid")
            if (
                self._current_root_identity(self.memory_dir, "memory root")
                != journal_root_identity
            ):
                raise ValueError("journal memory root identity does not match")
            journal_handoffs = payload.get("handoffs_dir")
            journal_handoffs_root = (
                Path(journal_handoffs).resolve() if journal_handoffs else None
            )
            handoffs_identity = payload.get("handoffs_root")
            if journal_handoffs_root is None:
                if handoffs_identity is not None:
                    raise ValueError("journal has a handoffs identity without a root")
                self.handoffs_dir = None
                self._handoffs_identity = None
            else:
                if not isinstance(handoffs_identity, dict):
                    raise ValueError("journal is missing the handoffs root identity")
                journal_handoffs_identity = (
                    handoffs_identity["device"],
                    handoffs_identity["inode"],
                )
                if not all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    for value in journal_handoffs_identity
                ) or journal_handoffs_identity[1] == 0:
                    raise ValueError("journal handoffs root identity is invalid")
                if (
                    self._current_root_identity(
                        journal_handoffs_root, "handoffs root"
                    )
                    != journal_handoffs_identity
                ):
                    raise ValueError("journal handoffs root identity does not match")
                self.handoffs_dir = journal_handoffs_root
                self._handoffs_identity = journal_handoffs_identity
            file_items = payload.get("files")
            move_items = payload.get("moves")
            if not isinstance(file_items, list) or not isinstance(move_items, list):
                raise ValueError("journal files and moves must be lists")
            self._files.clear()
            self._moves.clear()
            seen_quarantines: set[Path] = set()
            for item in file_items:
                if not isinstance(item, dict):
                    raise ValueError("journal file entry is not an object")
                path = self._inside(Path(item["path"]), self.memory_dir, "journal file")
                existing_file_paths = [record.path for record in self._files.values()]
                preflight_visible_path_aliases(
                    [*existing_file_paths, path],
                    label="journal managed visible",
                    check_existing_identities=False,
                )
                if any(
                    _same_visible_path_spelling(existing, path)
                    for existing in existing_file_paths
                ):
                    raise ValueError(f"duplicate journal file {path.name}")
                existed = item["existed"]
                if not isinstance(existed, bool):
                    raise ValueError(f"file {path.name} has invalid existed flag")
                encoded_original = item.get("original")
                if encoded_original is None:
                    original = None
                elif isinstance(encoded_original, str):
                    original = base64.b64decode(encoded_original, validate=True)
                else:
                    raise ValueError(f"file {path.name} original is invalid")
                original_identity = self._identity_from_payload(
                    item.get("original_identity"), f"original file {path.name}"
                )
                artifact = self._identity_from_payload(
                    item.get("artifact"), f"file {path.name}"
                )
                pending_artifact = self._identity_from_payload(
                    item.get("pending_artifact"), f"pending file {path.name}"
                )
                write_temp_identity = self._identity_from_payload(
                    item.get("write_temp_identity"),
                    f"atomic temp file {path.name}",
                )
                publish_previous = self._identity_from_payload(
                    item.get("publish_previous"),
                    f"previous visible file {path.name}",
                )
                original_mode = item.get("original_mode")
                if original_mode is not None and (
                    not isinstance(original_mode, int)
                    or isinstance(original_mode, bool)
                    or original_mode < 0
                    or original_mode > 0o7777
                ):
                    raise ValueError(f"file {path.name} mode is invalid")
                quarantine = self._inside(
                    Path(item["quarantine"]), self.memory_dir, "journal quarantine"
                )
                if not self._valid_private_path(quarantine, path.parent, ".mdq-"):
                    raise ValueError(f"file {path.name} quarantine is invalid")
                restore_temp = self._inside(
                    Path(item["restore_temp"]), self.memory_dir, "journal restore temp"
                )
                if not self._valid_private_path(restore_temp, path.parent, ".mdr-"):
                    raise ValueError(f"file {path.name} restore temp is invalid")
                write_temp_value = item.get("write_temp")
                if write_temp_value is None:
                    write_temp = None
                elif isinstance(write_temp_value, str):
                    write_temp = self._inside(
                        Path(write_temp_value),
                        self.memory_dir,
                        "journal atomic write temp",
                    )
                    atomic_prefix = f".{path.name}."
                    if (
                        write_temp.parent != path.parent
                        or not write_temp.name.startswith(atomic_prefix)
                        or not write_temp.name.endswith(".tmp")
                    ):
                        raise ValueError(
                            f"file {path.name} atomic write temp is invalid"
                        )
                else:
                    raise ValueError(f"file {path.name} atomic write temp is invalid")
                if (
                    quarantine == restore_temp
                    or quarantine in seen_quarantines
                    or restore_temp in seen_quarantines
                    or (write_temp is not None and write_temp in seen_quarantines)
                    or write_temp in (quarantine, restore_temp)
                ):
                    raise ValueError(f"file {path.name} recovery paths collide")
                seen_quarantines.update((quarantine, restore_temp))
                if write_temp is not None:
                    seen_quarantines.add(write_temp)
                state = item.get("state")
                if state not in {"planned", "quarantined", "done"}:
                    raise ValueError(f"file {path.name} state is invalid")
                if existed:
                    if (
                        original is None
                        or original_identity is None
                        or original_mode is None
                    ):
                        raise ValueError(f"file {path.name} original state is incomplete")
                    if hashlib.sha256(original).hexdigest() != original_identity.digest:
                        raise ValueError(f"file {path.name} original digest does not match")
                    if len(original) != original_identity.size:
                        raise ValueError(f"file {path.name} original size does not match")
                elif any(
                    value is not None
                    for value in (original, original_identity, original_mode)
                ):
                    raise ValueError(f"new file {path.name} has an original state")
                if write_temp is None:
                    if (
                        pending_artifact is not None
                        or write_temp_identity is not None
                        or publish_previous is not None
                    ):
                        raise ValueError(
                            f"file {path.name} has atomic write state without a temp path"
                        )
                elif write_temp_identity is None:
                    if pending_artifact is not None:
                        write_temp_identity = pending_artifact
                    else:
                        raise ValueError(
                            f"file {path.name} has an atomic temp without an identity"
                        )
                if phase == "committed" and (
                    state != "planned"
                    or artifact is None
                    or pending_artifact is not None
                    or write_temp is not None
                    or write_temp_identity is not None
                    or publish_previous is not None
                ):
                    raise ValueError(
                        f"committed file {path.name} has incomplete state"
                    )
                if phase == "rolled_back" and (
                    state != "done"
                    or pending_artifact is not None
                    or write_temp is not None
                    or write_temp_identity is not None
                    or publish_previous is not None
                ):
                    raise ValueError(
                        f"rolled-back file {path.name} has incomplete state"
                    )
                self._files[path] = _FileRecord(
                    path=path,
                    existed=existed,
                    original=original,
                    original_identity=original_identity,
                    original_mode=original_mode,
                    artifact=artifact,
                    pending_artifact=pending_artifact,
                    quarantine=quarantine,
                    restore_temp=restore_temp,
                    write_temp=write_temp,
                    write_temp_identity=write_temp_identity,
                    publish_previous=publish_previous,
                    state=state,
                )
            if move_items and journal_handoffs_root is None:
                raise ValueError("journal contains handoff moves without a handoffs root")
            seen_moves: list[tuple[Path, Path]] = []
            seen_move_paths: list[Path] = []
            for item in move_items:
                if not isinstance(item, dict):
                    raise ValueError("journal move entry is not an object")
                source = self._inside(
                    Path(item["source"]), journal_handoffs_root, "journal source"
                )
                destination = self._inside(
                    Path(item["destination"]), journal_handoffs_root, "journal destination"
                )
                move_key = (source, destination)
                if any(
                    _same_visible_path_spelling(previous_source, source)
                    and _same_visible_path_spelling(previous_destination, destination)
                    for previous_source, previous_destination in seen_moves
                ):
                    raise ValueError(f"duplicate journal move {source.name}")
                preflight_visible_path_aliases(
                    [*seen_move_paths, source, destination],
                    label="journal managed visible",
                    check_existing_identities=False,
                )
                seen_moves.append(move_key)
                if _same_visible_path_spelling(source, destination):
                    raise ValueError(
                        f"move {source.name} source and destination are identical"
                    )
                if any(
                    _same_visible_path_spelling(previous, current)
                    for previous in seen_move_paths
                    for current in (source, destination)
                ):
                    raise ValueError(
                        f"move {source.name} overlaps another managed handoff path"
                    )
                seen_move_paths.extend((source, destination))
                artifact = self._identity_from_payload(
                    item.get("artifact"), f"move artifact {source.name}"
                )
                if artifact is None:
                    raise ValueError(f"move {source.name} is missing its artifact")
                source_quarantine = self._inside(
                    Path(item["source_quarantine"]),
                    journal_handoffs_root,
                    "move source quarantine",
                )
                destination_quarantine = self._inside(
                    Path(item["destination_quarantine"]),
                    journal_handoffs_root,
                    "move destination quarantine",
                )
                if not self._valid_private_path(
                    source_quarantine, source.parent, ".mds-"
                ):
                    raise ValueError(f"move {source.name} source quarantine is invalid")
                if not self._valid_private_path(
                    destination_quarantine, destination.parent, ".mdd-"
                ):
                    raise ValueError(
                        f"move {source.name} destination quarantine is invalid"
                    )
                if (
                    source_quarantine == destination_quarantine
                    or source_quarantine in seen_quarantines
                    or destination_quarantine in seen_quarantines
                ):
                    raise ValueError(f"move {source.name} recovery paths collide")
                seen_quarantines.update(
                    (source_quarantine, destination_quarantine)
                )
                state = item.get("state")
                if state not in {"planned", "linked", "moved", "done"}:
                    raise ValueError(f"move {source.name} state is invalid")
                if phase == "committed" and state != "moved":
                    raise ValueError(
                        f"committed move {source.name} has incomplete state"
                    )
                if phase == "rolled_back" and state != "done":
                    raise ValueError(
                        f"rolled-back move {source.name} has incomplete state"
                    )
                self._moves.append(
                    _MoveRecord(
                        source=source,
                        destination=destination,
                        artifact=artifact,
                        source_quarantine=source_quarantine,
                        destination_quarantine=destination_quarantine,
                        state=state,
                    )
                )
            visible_paths = list(self._files)
            for record in self._moves:
                visible_paths.extend((record.source, record.destination))
            if len({_visible_path_alias_key(path) for path in visible_paths}) != len(
                visible_paths
            ):
                raise ValueError("journal managed visible paths overlap")
            try:
                preflight_visible_path_aliases(
                    visible_paths,
                    label="journal managed visible",
                    check_existing_identities=False,
                )
            except TransactionRecoveryError as exc:
                raise ValueError(str(exc)) from exc
            if set(visible_paths) & seen_quarantines:
                raise ValueError(
                    "journal recovery paths collide with managed visible paths"
                )
            alias_paths: dict[str, Path] = {}
            for path in (*visible_paths, *seen_quarantines):
                key = _visible_path_alias_key(path)
                previous = alias_paths.get(key)
                if previous is not None and not _same_visible_path_spelling(
                    previous, path
                ):
                    raise ValueError(
                        "journal recovery path aliases another managed path: "
                        f"{previous.name}, {path.name}"
                    )
                alias_paths[key] = path
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            OSError,
            TransactionError,
        ) as exc:
            raise TransactionRecoveryError(
                f"cannot read recovery journal {self._journal_path}: {exc}"
            ) from exc

    def _identity_if_present(
        self, path: Path, label: str
    ) -> _ArtifactIdentity | None:
        if not self._lexists(path):
            return None
        try:
            return self._artifact_identity(path)
        except OSError as exc:
            raise OSError(f"cannot inspect {label}: {exc}") from exc

    def _move_to_quarantine(
        self,
        path: Path,
        quarantine: Path,
        expected: _ArtifactIdentity | None,
        label: str,
    ) -> Path | None:
        if expected is None:
            raise OSError(
                f"cannot prove ownership of {label}; preserve it and resolve manually"
            )
        renamed_here = False
        if not self._lexists(quarantine):
            if not self._lexists(path):
                return None
            try:
                _rename_noreplace(path, quarantine)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise OSError(
                        f"cannot quarantine {label} at {quarantine.name}: {exc}"
                    ) from exc
            else:
                renamed_here = True
                _fsync_directory(path.parent)
        actual = self._identity_if_present(quarantine, f"quarantined {label}")
        if actual is None:
            raise OSError(
                f"quarantine for {label} is missing after rename; resolve manually"
            )
        if actual != expected:
            if renamed_here and not self._lexists(path):
                try:
                    _rename_noreplace(quarantine, path)
                    _fsync_directory(path.parent)
                except OSError as restore_exc:
                    raise OSError(
                        f"{label} no longer matches the transaction artifact; "
                        f"quarantine preserved at {quarantine.name}: {restore_exc}"
                    ) from restore_exc
            raise OSError(
                f"{label} no longer matches the transaction artifact; "
                "preserved for manual recovery"
            )
        return quarantine

    def _unlink_private_owned(
        self, path: Path, expected: _ArtifactIdentity, label: str
    ) -> None:
        """Delete only a proven artifact at a journaled private path.

        Public names are never compare-then-unlinked. The sole compare/unlink
        boundary is an unpredictable, same-directory quarantine name that was
        persisted before the atomic no-clobber rename. A collision or identity
        change keeps both the path and journal for manual recovery.
        """
        actual = self._identity_if_present(path, label)
        if actual is None:
            return
        if actual != expected:
            raise OSError(f"{label} changed; preserve it and resolve manually")
        path.unlink()
        _fsync_directory(path.parent)

    def _path_matches_original(self, path: Path, record: _FileRecord) -> bool:
        if (
            record.original is None
            or record.original_identity is None
            or record.original_mode is None
        ):
            return False
        try:
            content, identity, mode = self._snapshot_regular(path)
        except OSError:
            return False
        return (
            content == record.original
            and identity.digest == record.original_identity.digest
            and identity.size == record.original_identity.size
            and mode == record.original_mode
        )

    def _prepare_restore_temp(self, record: _FileRecord) -> None:
        if record.original is None or record.original_mode is None:
            raise OSError("journal is missing original file bytes or mode")
        if self._lexists(record.restore_temp):
            if not self._path_matches_original(record.restore_temp, record):
                raise OSError(
                    f"restore temp {record.restore_temp.name} changed; "
                    "preserve it and resolve manually"
                )
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(record.restore_temp, flags, record.original_mode)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(record.original)
                stream.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, record.original_mode)
                elif os.name == "nt":  # pragma: no cover - Windows only
                    os.chmod(record.restore_temp, record.original_mode)
                else:  # pragma: no cover - uncommon platform
                    raise OSError(errno.ENOTSUP, "file mode restore is unsupported")
                os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(record.restore_temp.parent)

    def _restore_original_exclusive(self, record: _FileRecord) -> None:
        self._prepare_restore_temp(record)
        if not self._lexists(record.path):
            try:
                os.link(record.restore_temp, record.path, follow_symlinks=False)
            except NotImplementedError as exc:
                raise OSError(
                    errno.ENOTSUP, "hard-link restore is unsupported"
                ) from exc
            except FileExistsError:
                pass
            _fsync_directory(record.path.parent)
        if not self._path_matches_original(record.path, record):
            raise OSError(
                f"replacement collision at {record.path.name}; original is preserved "
                f"at {record.restore_temp.name}"
            )

    def _cleanup_restore_temp(self, record: _FileRecord) -> None:
        if not self._lexists(record.restore_temp):
            return
        if not self._path_matches_original(record.restore_temp, record):
            raise OSError(
                f"restore temp {record.restore_temp.name} changed; resolve manually"
            )
        record.restore_temp.unlink()
        _fsync_directory(record.restore_temp.parent)

    def _reconcile_pending_write(self, record: _FileRecord) -> None:
        pending = record.pending_artifact
        temp_owner = record.write_temp_identity
        publish_previous = record.publish_previous
        if record.write_temp is None:
            if (
                temp_owner is not None
                or pending is not None
                or publish_previous is not None
            ):
                raise OSError(
                    f"pending write for {record.path.name} has no temp path; "
                    "preserve files and resolve manually"
                )
            return
        if temp_owner is None:
            raise OSError(
                f"atomic write temp {record.write_temp.name} has no ownership identity; "
                "preserve files and resolve manually"
            )

        path_identity = self._identity_if_present(
            record.path, f"current file {record.path.name}"
        )
        temp_identity = self._identity_if_present(
            record.write_temp, f"atomic write temp {record.write_temp.name}"
        )
        quarantine_identity = self._identity_if_present(
            record.quarantine, f"publish quarantine {record.quarantine.name}"
        )
        if quarantine_identity is not None:
            if publish_previous is None or quarantine_identity != publish_previous:
                raise OSError(
                    f"publish quarantine {record.quarantine.name} changed; "
                    "preserve files and resolve manually"
                )
        if temp_identity is not None:
            if (temp_identity.device, temp_identity.inode) != (
                temp_owner.device,
                temp_owner.inode,
            ):
                raise OSError(
                    f"atomic write temp {record.write_temp.name} changed; "
                    "preserve it and resolve manually"
                )
            if quarantine_identity is not None:
                if path_identity is not None:
                    raise OSError(
                        f"memory file {record.path.name} was recreated while its "
                        "previous version was quarantined; preserve all files and "
                        "resolve manually"
                    )
                try:
                    _rename_noreplace(record.quarantine, record.path)
                except OSError as exc:
                    raise OSError(
                        f"cannot restore previous memory file {record.path.name}; "
                        "preserve all files and resolve manually"
                    ) from exc
                _fsync_directory(record.path.parent)
                path_identity = publish_previous
                quarantine_identity = None
            previous_visible = record.artifact
            if previous_visible is None:
                previous_is_safe = (
                    self._path_matches_original(record.path, record)
                    if record.existed
                    else path_identity is None
                )
            else:
                previous_is_safe = path_identity == previous_visible
            if not previous_is_safe:
                raise OSError(
                    f"current file {record.path.name} changed while an atomic "
                    "write was pending; preserve both and resolve manually"
                )
            self._unlink_private_owned(
                record.write_temp,
                temp_identity,
                f"atomic write temp {record.write_temp.name}",
            )
        elif pending is not None and path_identity == pending:
            record.artifact = pending
            if quarantine_identity is not None:
                self._unlink_private_owned(
                    record.quarantine,
                    quarantine_identity,
                    f"previous memory file {record.quarantine.name}",
                )
        elif record.artifact is not None and path_identity == record.artifact:
            if quarantine_identity is not None:
                raise OSError(
                    f"publish quarantine {record.quarantine.name} remains beside "
                    f"memory file {record.path.name}; preserve both and resolve manually"
                )
            pass
        elif record.artifact is None and (
            (record.existed and self._path_matches_original(record.path, record))
            or (not record.existed and path_identity is None)
        ):
            if quarantine_identity is not None:
                raise OSError(
                    f"publish quarantine {record.quarantine.name} remains after "
                    "the visible file was restored; preserve both and resolve manually"
                )
            pass
        else:
            raise OSError(
                f"pending write for {record.path.name} cannot be reconciled; "
                "preserve files and resolve manually"
            )
        record.pending_artifact = None
        record.write_temp = None
        record.write_temp_identity = None
        record.publish_previous = None
        self._write_journal()

    def _rollback_file(self, record: _FileRecord) -> None:
        self._reconcile_pending_write(record)
        if record.state == "done":
            self._validate_rolled_back_file(record)
            return
        path_identity = self._identity_if_present(
            record.path, f"current file {record.path.name}"
        )
        quarantine_identity = self._identity_if_present(
            record.quarantine, f"quarantine {record.quarantine.name}"
        )

        if quarantine_identity is None:
            if path_identity is None:
                if record.existed:
                    self._restore_original_exclusive(record)
            elif record.artifact is not None and path_identity == record.artifact:
                self._move_to_quarantine(
                    record.path,
                    record.quarantine,
                    record.artifact,
                    f"current file {record.path.name}",
                )
                record.state = "quarantined"
                self._write_journal()
            elif record.existed and self._path_matches_original(record.path, record):
                pass
            elif (
                not record.existed
                and record.state == "quarantined"
                and record.artifact is not None
            ):
                # A prior cleanup removed the owned quarantine before its
                # durable state update. Any public name now is a later
                # replacement and must be preserved.
                record.state = "done"
                self._write_journal()
                self._validate_rolled_back_file(record)
                return
            elif not record.existed and record.artifact is not None:
                raise OSError(
                    f"current file {record.path.name} no longer matches the "
                    "transaction artifact; refusing to delete the replacement, "
                    "resolve manually"
                )
            else:
                raise OSError(
                    f"current file {record.path.name} no longer matches or cannot "
                    "be proven to be the transaction artifact; preserve the "
                    "replacement and resolve manually"
                )

        quarantine_identity = self._identity_if_present(
            record.quarantine, f"quarantine {record.quarantine.name}"
        )
        if quarantine_identity is not None and (
            record.artifact is None or quarantine_identity != record.artifact
        ):
            raise OSError(
                f"quarantine {record.quarantine.name} does not match the "
                "transaction artifact; resolve manually"
            )

        if quarantine_identity is not None and record.existed:
            if self._lexists(record.path):
                if not self._path_matches_original(record.path, record):
                    raise OSError(
                        f"replacement collision at {record.path.name}; quarantine "
                        f"preserved at {record.quarantine.name}"
                    )
            else:
                self._restore_original_exclusive(record)
        elif quarantine_identity is not None and self._lexists(record.path):
            raise OSError(
                f"replacement collision at {record.path.name}; quarantine "
                f"preserved at {record.quarantine.name}"
            )
        record.state = "done"
        self._write_journal()
        self._validate_rolled_back_file(record)

    def _quarantine_and_remove_destination(self, record: _MoveRecord) -> None:
        self._move_to_quarantine(
            record.destination,
            record.destination_quarantine,
            record.artifact,
            f"processed handoff {record.destination.name}",
        )

    def _rollback_move(self, record: _MoveRecord) -> None:
        if record.state == "done":
            self._validate_rolled_back_move(record)
            return
        source_q_identity = self._identity_if_present(
            record.source_quarantine,
            f"source quarantine {record.source_quarantine.name}",
        )
        destination_q_identity = self._identity_if_present(
            record.destination_quarantine,
            f"destination quarantine {record.destination_quarantine.name}",
        )
        if source_q_identity is not None and source_q_identity != record.artifact:
            raise OSError("handoff source quarantine changed; resolve manually")
        if destination_q_identity is not None and destination_q_identity != record.artifact:
            raise OSError("handoff destination quarantine changed; resolve manually")

        source_identity = self._identity_if_present(
            record.source, f"handoff source {record.source.name}"
        )
        destination_identity = self._identity_if_present(
            record.destination, f"processed handoff {record.destination.name}"
        )
        if destination_q_identity is not None and destination_identity is not None:
            raise OSError(
                "processed handoff was replaced while its quarantine exists; "
                "resolve manually"
            )
        if destination_identity is not None:
            if destination_identity != record.artifact:
                raise OSError("processed handoff no longer matches; resolve manually")
            self._quarantine_and_remove_destination(record)
            destination_q_identity = record.artifact

        private_source = None
        if source_q_identity == record.artifact:
            private_source = record.source_quarantine
        elif destination_q_identity == record.artifact:
            private_source = record.destination_quarantine

        if source_identity is None and private_source is None:
            raise OSError("both source and destination are missing; resolve manually")
        if source_identity is None:
            assert private_source is not None
            try:
                os.link(private_source, record.source, follow_symlinks=False)
            except NotImplementedError as exc:
                raise OSError(
                    errno.ENOTSUP, "hard-link handoff restore is unsupported"
                ) from exc
            except FileExistsError:
                pass
            _fsync_directory(record.source.parent)
            if self._artifact_identity(record.source) != record.artifact:
                raise OSError(
                    "handoff source changed during restore; resolve manually"
                )
        elif source_identity != record.artifact:
            raise OSError(
                "handoff source was replaced; original preserved privately, "
                "resolve manually"
            )
        record.state = "done"
        self._write_journal()
        self._validate_rolled_back_move(record)

    def _validate_rolled_back_file(self, record: _FileRecord) -> None:
        if (
            record.pending_artifact is not None
            or record.write_temp is not None
            or record.write_temp_identity is not None
            or record.publish_previous is not None
        ):
            raise OSError(f"file {record.path.name} still has a pending write")
        if record.existed:
            if not self._path_matches_original(record.path, record):
                raise OSError(
                    f"restored file {record.path.name} changed before rollback completed; "
                    "private original retained"
                )
        elif self._lexists(record.path) and self._lexists(record.quarantine):
            raise OSError(
                f"new file name {record.path.name} was recreated before rollback completed"
            )

    def _validate_rolled_back_move(self, record: _MoveRecord) -> None:
        source_identity = self._identity_if_present(
            record.source, f"restored handoff {record.source.name}"
        )
        if source_identity != record.artifact:
            raise OSError(
                f"restored handoff {record.source.name} changed before rollback completed"
            )
        if self._lexists(record.destination):
            raise OSError(
                f"processed handoff {record.destination.name} reappeared before rollback completed"
            )

    def _rollback(self, *, recovering: bool) -> None:
        del recovering
        self._validate_current_roots()
        errors: list[str] = []
        for record in reversed(self._moves):
            try:
                self._rollback_move(record)
            except (OSError, TransactionError) as exc:
                errors.append(f"restore {record.source.name}: {exc}")
        for record in reversed(list(self._files.values())):
            try:
                self._rollback_file(record)
            except (OSError, TransactionError) as exc:
                errors.append(f"restore {record.path.name}: {exc}")
        if errors:
            raise TransactionRecoveryError("; ".join(errors))
        self._phase = "rolled_back"
        try:
            self._write_journal()
        except OSError as exc:
            raise TransactionRecoveryError(
                "rollback restored visible paths but its completion marker could "
                "not be made durable; private recovery artifacts retained"
            ) from exc
        self._cleanup_rolled_back_journal()

    def _cleanup_rolled_back_journal(self) -> None:
        if self._phase != "rolled_back":
            raise TransactionRecoveryError(
                "refusing rolled-back cleanup for an active transaction"
            )
        try:
            for record in self._moves:
                self._validate_rolled_back_move(record)
            for record in self._files.values():
                self._validate_rolled_back_file(record)

            for record in self._moves:
                self._unlink_private_owned(
                    record.source_quarantine,
                    record.artifact,
                    f"source quarantine {record.source_quarantine.name}",
                )
                self._unlink_private_owned(
                    record.destination_quarantine,
                    record.artifact,
                    f"destination quarantine {record.destination_quarantine.name}",
                )
            for record in self._files.values():
                if record.artifact is not None:
                    self._unlink_private_owned(
                        record.quarantine,
                        record.artifact,
                        f"quarantine {record.quarantine.name}",
                    )
                self._cleanup_restore_temp(record)
            self._clear_journal()
        except TransactionRecoveryError:
            raise
        except OSError as exc:
            raise TransactionRecoveryError(
                "rollback completed but private recovery cleanup is incomplete; "
                f"journal retained: {exc}"
            ) from exc
        self._files.clear()
        self._moves.clear()
        self._watched_files.clear()
        self._phase = "active"

    def _clear_journal(self) -> None:
        try:
            self._journal_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise TransactionRecoveryError(
                f"cannot remove recovery journal: {exc}"
            ) from exc
        try:
            _fsync_directory(self._state_dir)
        except OSError as exc:
            raise TransactionRecoveryError(
                f"recovery journal removal is not durable: {exc}"
            ) from exc

    def _release_lock(self) -> None:
        """Release the process lock; exposed only to simulate a crashed owner in tests."""
        if self._lock_file is None:
            return
        active_exception = sys.exc_info()[0] is not None
        release_error: OSError | None = None
        try:
            if self._lock_acquired:
                if fcntl is not None:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - exercised on Windows
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError as exc:
            release_error = exc
        finally:
            try:
                self._lock_file.close()
            except OSError as exc:
                if release_error is None:
                    release_error = exc
            finally:
                self._lock_file = None
                self._lock_acquired = False
        if release_error is not None and not active_exception:
            raise release_error

    def _acquire_lock(self) -> None:
        if fcntl is not None:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)
            return
        # Windows byte-range locks require the byte to exist.
        self._lock_file.seek(0, os.SEEK_END)  # pragma: no cover - Windows only
        if self._lock_file.tell() == 0:  # pragma: no cover - Windows only
            self._lock_file.write(b"\0")
            self._lock_file.flush()
        retry_errnos = {
            errno.EACCES,
            getattr(errno, "EDEADLK", errno.EACCES),
        }
        while True:  # pragma: no cover - exercised on Windows
            self._lock_file.seek(0)
            try:
                msvcrt.locking(
                    self._lock_file.fileno(), msvcrt.LK_NBLCK, 1
                )
                return
            except OSError as exc:
                if exc.errno not in retry_errnos:
                    raise
                time.sleep(0.05)
