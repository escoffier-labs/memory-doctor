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

# The Claude Code harness silently DROPS MEMORY.md content read beyond a
# ~24.4KB limit, so an index that is fine on the line threshold can still have
# its tail invisible to the agent. 24000 is a safe default just under that
# observed ~24.4KB read ceiling. Unlike the line budget this is harness
# behavior, not a brigade.budgets value, so it is owned here.
DEFAULT_MAX_BYTES = 24000


# Handoff directories ship a TEMPLATE.md describing the format. It is not a
# handoff: it has placeholder values in every field, so the parser rejects it,
# and counting it makes `status` report a pending handoff that never clears.
# On this fleet that showed as "1 pending, oldest 114.8 days" for the entire
# life of the directory.
HANDOFF_TEMPLATE_NAMES = frozenset({"template.md"})


def iter_pending_handoffs(handoffs_dir):
    """Sorted *.md in the handoff inbox, excluding the format template."""
    return sorted(
        path
        for path in handoffs_dir.glob("*.md")
        if path.name.lower() not in HANDOFF_TEMPLATE_NAMES
    )


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
    max_bytes: int = DEFAULT_MAX_BYTES
    # Hooks shorter than this stay full in the index; longer ones are
    # "tighten" candidates whose full text is moved into the linked card.
    max_hook_chars: int = 140


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
    max_bytes: int | None = None,
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
    if max_bytes is not None:
        nbytes = max_bytes
    else:
        env_b = os.environ.get("MEMORY_DOCTOR_MAX_BYTES")
        try:
            nbytes = int(env_b) if env_b else DEFAULT_MAX_BYTES
        except ValueError:
            raise PathConfigError(
                f"MEMORY_DOCTOR_MAX_BYTES must be an integer, got: {env_b!r}"
            ) from None
    if nbytes <= 0:
        raise PathConfigError(f"max bytes must be greater than 0, got: {nbytes}")
    return PathConfig(memory_dir=md, handoffs_dir=hd, max_lines=lines, max_bytes=nbytes)
