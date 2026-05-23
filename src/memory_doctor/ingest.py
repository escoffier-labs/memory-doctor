"""Ingest verb: promote pending handoffs into cards."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from memory_doctor.git import (
    commit_run,
    files_have_uncommitted_changes,
    is_git_repo,
    working_tree_sane,
)
from memory_doctor.parsing import HandoffParseError, ParsedHandoff, parse_handoff
from memory_doctor.paths import PathConfig
from memory_doctor.safety import (
    UnsafeTargetError,
    atomic_write_text,
    resolve_card_target,
)


def _process_handoff(
    parsed: ParsedHandoff,
    memory_dir: Path,
    handoffs_dir: Path,
    *,
    apply: bool,
    force: bool,
    touched: list[tuple[Path, str]],
) -> tuple[str, bool]:
    """Returns (message, success). Appends (target_path, reason) to `touched`
    on each successful write so the caller can build the commit body."""
    src = parsed.path

    if parsed.action == "no-card":
        msg = f"{src.name}: no-card -> move to processed"
        if apply:
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    try:
        target = resolve_card_target(memory_dir, parsed.target)
    except UnsafeTargetError as e:
        return (f"{src.name}: SKIP - unsafe target {parsed.target!r}: {e}", False)

    if parsed.action == "create-card":
        if target.exists():
            existing = target.read_text()
            if existing.strip() == parsed.content.strip():
                msg = f"{src.name}: create-card -> {target.name} already identical, move to processed"
                if apply:
                    shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
                return msg, True
            if not force:
                return (f"{src.name}: SKIP - {target.name} exists with different content (use --force)", False)
            msg = f"{src.name}: create-card -> {target.name} (FORCE overwrite)"
            if apply:
                payload = parsed.content if parsed.content.endswith("\n") else parsed.content + "\n"
                atomic_write_text(target, payload)
                touched.append((target, f"create-card (force) from {src.name}"))
                shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
            return msg, True
        msg = f"{src.name}: create-card -> {target.name}"
        if apply:
            payload = parsed.content if parsed.content.endswith("\n") else parsed.content + "\n"
            atomic_write_text(target, payload)
            touched.append((target, f"create-card from {src.name}"))
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    if parsed.action == "update-card":
        if not target.exists():
            return (f"{src.name}: ERROR - update-card target {target.name} does not exist", False)
        msg = f"{src.name}: update-card -> {target.name} (append)"
        if apply:
            existing = target.read_text()
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            atomic_write_text(target, existing + sep + parsed.content + "\n")
            touched.append((target, f"update-card append from {src.name}"))
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    return (f"{src.name}: unknown action {parsed.action!r}", False)


def _preflight_for_commit(memory_dir: Path, planned_targets: list[Path]) -> int:
    """Run the three pre-flight checks from the spec. 0 = ok, 2 = abort.

    Called BEFORE any file write when --commit is set, so a failure leaves
    the on-disk state untouched.
    """
    if not is_git_repo(memory_dir):
        print(
            f"memory-doctor: --commit requires the memory dir to be a git repo\n"
            f"  memory dir: {memory_dir}\n"
            f"  fix: run `memory-doctor init-git` once, then retry",
            file=sys.stderr,
        )
        return 2

    ok, reason = working_tree_sane(memory_dir)
    if not ok:
        print(
            f"memory-doctor: refusing to commit, git is in the middle of a {reason}\n"
            f"  fix: complete or abort the in-progress operation, then retry",
            file=sys.stderr,
        )
        return 2

    dirty = files_have_uncommitted_changes(memory_dir, planned_targets)
    if dirty:
        print(
            "memory-doctor: refusing to commit, target files have uncommitted local changes:",
            file=sys.stderr,
        )
        for path, status in dirty:
            print(f"  - {path.name} ({status})", file=sys.stderr)
        print(
            "  fix: review with `git diff`, commit/stash/discard, then retry",
            file=sys.stderr,
        )
        return 2

    return 0


def _plan_targets(pending: list[Path], memory_dir: Path) -> list[Path]:
    """Resolve target file paths from each pending handoff for pre-flight checks.

    Skips handoffs that fail to parse or have unsafe targets; the main pass
    will report those properly. Pre-flight just needs the set of files that
    might be written.
    """
    targets: list[Path] = []
    for p in pending:
        try:
            parsed = parse_handoff(p)
        except HandoffParseError:
            continue
        if parsed.action == "no-card":
            continue
        try:
            targets.append(resolve_card_target(memory_dir, parsed.target))
        except UnsafeTargetError:
            continue
    return targets


def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    force: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    if commit and not apply:
        # Friendlier than erroring: people experimenting with the flag often
        # forget --apply, and the message guides them to the right thing.
        print("memory-doctor ingest: skipping commit (dry-run; use --apply)")

    if apply:
        (cfg.handoffs_dir / "processed").mkdir(exist_ok=True)

    pending = sorted(p for p in cfg.handoffs_dir.glob("*.md"))
    if not pending:
        print("memory-doctor ingest: no pending handoffs")
        return 0

    if apply and commit:
        planned = _plan_targets(pending, cfg.memory_dir)
        rc = _preflight_for_commit(cfg.memory_dir, planned)
        if rc != 0:
            return rc

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor ingest ({mode}): {len(pending)} handoff(s)")
    touched: list[tuple[Path, str]] = []
    all_ok = True
    promoted = 0
    skipped = 0
    for p in pending:
        try:
            parsed = parse_handoff(p)
        except HandoffParseError as e:
            print(f"  {p.name}: PARSE ERROR - {e}")
            all_ok = False
            skipped += 1
            continue
        msg, ok = _process_handoff(parsed, cfg.memory_dir, cfg.handoffs_dir, apply=apply, force=force, touched=touched)
        print(f"  {msg}")
        if ok:
            promoted += 1
        else:
            skipped += 1
            all_ok = False

    if apply and commit and touched:
        if skipped == 0:
            subject = f"memory-doctor ingest: {promoted} handoff{'s' if promoted != 1 else ''} promoted"
        else:
            subject = (
                f"memory-doctor ingest: {promoted} handoff{'s' if promoted != 1 else ''} promoted, "
                f"{skipped} skipped"
            )
        body = "\n".join(f"- {t.name} ({reason})" for t, reason in touched)
        result = commit_run(
            memory_dir=cfg.memory_dir,
            files=[t for t, _ in touched],
            subject=subject,
            body=body,
            author=commit_author,
        )
        if result.error_kind is None:
            print(f"\nCommitted {result.sha}")
        elif result.error_kind == "hook":
            print(
                "\nerror: pre-commit hook rejected the commit; your file changes are staged but not committed",
                file=sys.stderr,
            )
            print(f"  files: {', '.join(t.name for t, _ in touched)}", file=sys.stderr)
            print(f"  details: {result.error_message}", file=sys.stderr)
            return 1
        else:
            print(f"\nerror: commit failed ({result.error_kind}): {result.error_message}", file=sys.stderr)
            return 1
    elif apply and commit and not touched:
        print("\nno changes to commit")

    return 0 if all_ok else 1
