"""Path safety: ensure resolved target stays inside memory_dir.

Also exposes `atomic_write_text`, a crash-safe writer used by any verb that
mutates files on disk (compact, ingest).
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import unicodedata
from pathlib import Path


class UnsafeTargetError(Exception):
    pass


_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP")
    if hasattr(errno, name)
)


def _fsync_directory(path: Path) -> None:
    """Persist a replaced directory entry on platforms that support it."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(fd)


def atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically via tempfile + os.replace.

    Same-dir tempfile guarantees the rename is atomic on POSIX filesystems,
    so a crash mid-write leaves either the old or the new file, never a
    truncated mix.
    """
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    replaced = False
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            if existing_mode is not None:
                os.chmod(tmp, existing_mode)
            os.fsync(f.fileno())
        os.replace(tmp, path)
        replaced = True
        _fsync_directory(path.parent)
    except Exception:
        if not replaced:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def resolve_card_target(memory_dir: Path, raw: str) -> Path:
    """Resolve `raw` (a filename, possibly with leading 'cards/') against
    `memory_dir`, ensuring the result stays inside it. Rejects absolute paths,
    parent-dir traversal (..), any path that escapes the memory dir, and
    reserved names (the MEMORY.md index and git metadata) that a card write
    must never touch.
    """
    if not raw or raw.strip() != raw:
        raise UnsafeTargetError(f"empty or whitespace-padded target: {raw!r}")
    if raw.startswith("/"):
        raise UnsafeTargetError(f"absolute paths not allowed: {raw!r}")
    if "\\" in raw:
        # A backslash is a path separator on Windows; letting it through would
        # allow nested paths (and nested reserved names) on that platform.
        raise UnsafeTargetError(f"backslash not allowed in target: {raw!r}")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in raw):
        # Control/format chars (zero-width space, RTL override, ...) survive
        # str.strip() and can spoof a reserved name that the checks below
        # would otherwise catch.
        raise UnsafeTargetError(f"control or format characters not allowed: {raw!r}")
    if raw.startswith("cards/"):
        raw = raw[len("cards/"):]
    lowered = raw.lower()
    if lowered == "memory.md":
        raise UnsafeTargetError(f"reserved target (memory index): {raw!r}")
    if lowered.startswith(".git"):
        raise UnsafeTargetError(f"reserved target (git metadata): {raw!r}")
    if ".." in raw.split("/"):
        raise UnsafeTargetError(f"path traversal not allowed: {raw!r}")
    if "/" in raw:
        raise UnsafeTargetError(f"nested paths not allowed (flat cards only): {raw!r}")
    unresolved = memory_dir / raw
    if unresolved.is_symlink():
        # resolve() below follows symlinks, so a symlinked card would let one
        # name write through to another file (including the reserved ones).
        raise UnsafeTargetError(f"symlink targets not allowed: {raw!r}")
    candidate = unresolved.resolve()
    md = memory_dir.resolve()
    try:
        candidate.relative_to(md)
    except ValueError:
        raise UnsafeTargetError(f"target escapes memory dir: {raw!r}") from None
    # Belt and braces: re-check reserved names on the RESOLVED name in case a
    # future refactor reintroduces a rename/aliasing path above.
    resolved_lower = candidate.name.lower()
    if resolved_lower == "memory.md" or resolved_lower.startswith(".git"):
        raise UnsafeTargetError(f"target resolves to a reserved file: {raw!r}")
    return candidate
