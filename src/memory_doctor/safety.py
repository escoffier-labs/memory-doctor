"""Path safety: ensure resolved target stays inside memory_dir."""
from __future__ import annotations

from pathlib import Path


class UnsafeTargetError(Exception):
    pass


def resolve_card_target(memory_dir: Path, raw: str) -> Path:
    """Resolve `raw` (a filename, possibly with leading 'cards/') against
    `memory_dir`, ensuring the result stays inside it. Rejects absolute paths,
    parent-dir traversal (..), and any path that escapes the memory dir.
    """
    if not raw or raw.strip() != raw:
        raise UnsafeTargetError(f"empty or whitespace-padded target: {raw!r}")
    if raw.startswith("/"):
        raise UnsafeTargetError(f"absolute paths not allowed: {raw!r}")
    if raw.startswith("cards/"):
        raw = raw[len("cards/"):]
    if ".." in raw.split("/"):
        raise UnsafeTargetError(f"path traversal not allowed: {raw!r}")
    if "/" in raw:
        raise UnsafeTargetError(f"nested paths not allowed (flat cards only): {raw!r}")
    candidate = (memory_dir / raw).resolve()
    md = memory_dir.resolve()
    try:
        candidate.relative_to(md)
    except ValueError:
        raise UnsafeTargetError(f"target escapes memory dir: {raw!r}") from None
    return candidate
