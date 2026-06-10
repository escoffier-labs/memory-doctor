"""Path resolution for memory + handoffs dirs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The MEMORY.md index line limit is owned by brigade.budgets (the canonical
# source of truth shared across the escoffier-labs tooling). brigade-cli is a
# hard dependency, so this import normally succeeds; the fallback exists only
# for resilience (e.g. partial installs). Keep the fallback value in sync with
# brigade.budgets.MEMORY_INDEX_MAX_LINES, which remains the canonical source.
try:
    from brigade.budgets import MEMORY_INDEX_MAX_LINES as DEFAULT_MAX_LINES
except ImportError:  # pragma: no cover - brigade-cli is a declared dependency
    DEFAULT_MAX_LINES = 180


def _default_memory_dir() -> str:
    """Derive Claude Code's per-project memory dir from $HOME.

    Claude Code stores memory under ~/.claude/projects/<slug>/memory where
    <slug> is the user's home directory with each '/' replaced by '-'.
    e.g. /home/alice -> -home-alice
    """
    home = os.path.expanduser("~")
    slug = home.replace("/", "-")
    return f"~/.claude/projects/{slug}/memory"


DEFAULT_MEMORY_DIR = _default_memory_dir()
DEFAULT_HANDOFFS_DIR = "~/.openclaw/workspace/.claude/memory-handoffs"


class PathConfigError(Exception):
    pass


@dataclass(frozen=True)
class PathConfig:
    memory_dir: Path
    handoffs_dir: Path
    max_lines: int


def _resolve_dir(flag: str | None, env_key: str, default: str, label: str) -> Path:
    raw = flag or os.environ.get(env_key) or default
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        raise PathConfigError(f"{label} dir not found: {p}")
    if not p.is_dir():
        raise PathConfigError(f"{label} path is not a directory: {p}")
    return p


def resolve_paths(
    *,
    memory_dir: str | None,
    handoffs_dir: str | None,
    max_lines: int | None,
) -> PathConfig:
    md = _resolve_dir(memory_dir, "MEMORY_DOCTOR_MEMORY_DIR", DEFAULT_MEMORY_DIR, "memory")
    hd = _resolve_dir(handoffs_dir, "MEMORY_DOCTOR_HANDOFFS_DIR", DEFAULT_HANDOFFS_DIR, "handoffs")
    if max_lines is not None:
        lines = max_lines
    else:
        env = os.environ.get("MEMORY_DOCTOR_MAX_LINES")
        try:
            lines = int(env) if env else DEFAULT_MAX_LINES
        except ValueError:
            raise PathConfigError(
                f"MEMORY_DOCTOR_MAX_LINES must be an integer, got: {env!r}"
            ) from None
    if lines <= 0:
        raise PathConfigError(f"max lines must be greater than 0, got: {lines}")
    return PathConfig(memory_dir=md, handoffs_dir=hd, max_lines=lines)
