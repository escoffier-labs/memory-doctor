"""Command-line interface for memory-doctor."""
from __future__ import annotations

import argparse
import sys

from memory_doctor import __version__
from memory_doctor.paths import PathConfigError, resolve_paths


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memory-dir", default=None, help="Memory dir (cards + MEMORY.md). Default: ~/.claude/projects/-home-clawdbot/memory")
    p.add_argument("--handoffs-dir", default=None, help="Handoffs dir. Default: ~/.openclaw/workspace/.claude/memory-handoffs")
    p.add_argument("--max-lines", type=int, default=None, help="MEMORY.md threshold (default 180)")


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="memory-doctor", description="Maintenance CLI for the Claude Code / OpenClaw memory system.")
    root.add_argument("--version", action="version", version=f"memory-doctor {__version__}")
    sub = root.add_subparsers(dest="verb", required=True)

    p_status = sub.add_parser("status", help="Print a read-only summary")
    _add_common(p_status)
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of human text")

    p_lint = sub.add_parser("lint", help="Scan for dead [[wiki-links]]; exit 1 if any")
    _add_common(p_lint)

    p_ingest = sub.add_parser("ingest", help="Promote pending handoffs into cards")
    _add_common(p_ingest)
    p_ingest.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")
    p_ingest.add_argument("--force", action="store_true", help="Overwrite existing cards on create-card conflict")

    p_compact = sub.add_parser("compact", help="Flatten multi-line MEMORY.md entries into topic files")
    _add_common(p_compact)
    p_compact.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")

    return root


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = resolve_paths(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
        )
    except PathConfigError as e:
        print(f"memory-doctor: {e}", file=sys.stderr)
        return 2

    if args.verb == "status":
        from memory_doctor.status import run as run_status
        return run_status(cfg, as_json=args.json)
    if args.verb == "lint":
        from memory_doctor.lint import run as run_lint
        return run_lint(cfg)
    if args.verb == "ingest":
        from memory_doctor.ingest import run as run_ingest
        return run_ingest(cfg, apply=args.apply, force=args.force)
    if args.verb == "compact":
        from memory_doctor.compact import run as run_compact
        return run_compact(cfg, apply=args.apply)
    parser.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
