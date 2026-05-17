"""Path resolution for memory + handoffs dirs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MEMORY_DIR = "~/.claude/projects/-home-clawdbot/memory"
DEFAULT_HANDOFFS_DIR = "~/.openclaw/workspace/.claude/memory-handoffs"
DEFAULT_MAX_LINES = 180


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
        lines = int(env) if env else DEFAULT_MAX_LINES
    return PathConfig(memory_dir=md, handoffs_dir=hd, max_lines=lines)
