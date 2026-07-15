"""Command-line interface for memory-doctor."""
from __future__ import annotations

import argparse
import os
import sys

from memory_doctor import __version__
from memory_doctor.paths import PathConfigError, resolve_paths


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memory-dir", default=None, help="Memory dir (cards + MEMORY.md).")
    p.add_argument("--handoffs-dir", default=None, help="Handoffs dir.")
    p.add_argument("--max-lines", type=int, default=None, help="MEMORY.md line threshold (default 180)")
    p.add_argument("--max-bytes", type=int, default=None, help="MEMORY.md byte threshold (default 24000)")


def _add_commit_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--commit", action="store_true", help="Stage + commit after --apply (off by default).")
    p.add_argument("--no-commit", action="store_true", help="Suppress committing even if MEMORY_DOCTOR_COMMIT=1.")
    p.add_argument(
        "--commit-author", default=None,
        help='Override author for this commit ("Name <email>"). Default: git config user.name/user.email.',
    )


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
    _add_commit_flags(p_ingest)
    p_ingest.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")
    p_ingest.add_argument("--force", action="store_true", help="Overwrite existing cards on create-card conflict")

    p_compact = sub.add_parser("compact", help="Flatten multi-line MEMORY.md entries into topic files")
    _add_common(p_compact)
    _add_commit_flags(p_compact)
    p_compact.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")

    p_init = sub.add_parser("init-git", help="Initialize the memory dir as a git repo with one initial commit")
    _add_common(p_init)

    return root


def _resolve_commit_flag(args) -> bool:
    """Resolve commit intent from flag + env. --no-commit always wins."""
    if getattr(args, "no_commit", False):
        return False
    if getattr(args, "commit", False):
        return True
    return os.environ.get("MEMORY_DOCTOR_COMMIT", "").strip() in ("1", "true", "yes")


def _env_enabled_commit(args) -> bool:
    """True when the environment, rather than a CLI flag, enabled commits."""
    return (
        not getattr(args, "no_commit", False)
        and not getattr(args, "commit", False)
        and _resolve_commit_flag(args)
    )


def _resolve_commit_author(args) -> str | None:
    cli_author = getattr(args, "commit_author", None)
    if cli_author is not None:
        return cli_author
    return os.environ.get("MEMORY_DOCTOR_COMMIT_AUTHOR")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = resolve_paths(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
            max_bytes=args.max_bytes,
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
        commit = _resolve_commit_flag(args)
        if _env_enabled_commit(args):
            print(
                "memory-doctor: notice: commit mode enabled by "
                "MEMORY_DOCTOR_COMMIT (use --no-commit to disable)",
                file=sys.stderr,
            )
        return run_ingest(
            cfg, apply=args.apply, force=args.force,
            commit=commit,
            commit_author=_resolve_commit_author(args),
        )
    if args.verb == "compact":
        from memory_doctor.compact import run as run_compact
        commit = _resolve_commit_flag(args)
        if _env_enabled_commit(args):
            print(
                "memory-doctor: notice: commit mode enabled by "
                "MEMORY_DOCTOR_COMMIT (use --no-commit to disable)",
                file=sys.stderr,
            )
        return run_compact(
            cfg, apply=args.apply,
            commit=commit,
            commit_author=_resolve_commit_author(args),
        )
    if args.verb == "init-git":
        from memory_doctor.init_git import run as run_init_git
        return run_init_git(cfg)
    parser.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
