"""Ingest verb: promote pending handoffs into cards."""
from __future__ import annotations

import sys
from pathlib import Path

from memory_doctor.git import (
    GitStatusError,
    commit_run,
    files_have_uncommitted_changes,
    is_git_repo,
    validate_author_format,
    working_tree_sane,
)
from memory_doctor.parsing import HandoffParseError, ParsedHandoff, parse_handoff
from memory_doctor.paths import PathConfig, iter_pending_handoffs
from memory_doctor.safety import (
    UnsafeTargetError,
    atomic_write_text,
    resolve_card_target,
)
from memory_doctor.transaction import (
    ApplyTransaction,
    TransactionRecoveryError,
    has_pending_transaction_recovery,
    preflight_managed_artifact,
    preflight_transaction_capabilities,
    preflight_visible_path_aliases,
)


def _process_handoff(
    parsed: ParsedHandoff,
    memory_dir: Path,
    handoffs_dir: Path,
    *,
    apply: bool,
    force: bool,
    touched: list[tuple[Path, str]],
    transaction: ApplyTransaction | None,
    source_identity=None,
) -> tuple[str, bool]:
    """Returns (message, success). Appends (target_path, reason) to `touched`
    on each successful write so the caller can build the commit body."""
    src = parsed.path

    def move_processed() -> None:
        destination = handoffs_dir / "processed" / src.name
        if transaction is None:
            raise TransactionRecoveryError(
                "applied handoff move requires an active transaction"
            )
        transaction.move_handoff(
            src,
            destination,
            expected_identity=source_identity,
        )

    def write_target(target: Path, payload: str, expected_identity=None) -> None:
        if transaction:
            transaction.write_text(
                target,
                payload,
                expected_identity=expected_identity,
            )
        else:
            atomic_write_text(target, payload)

    if parsed.action == "no-card":
        msg = f"{src.name}: no-card -> move to processed"
        if apply:
            move_processed()
        return msg, True

    try:
        target = resolve_card_target(memory_dir, parsed.target)
    except UnsafeTargetError as e:
        return (f"{src.name}: SKIP - unsafe target {parsed.target!r}: {e}", False)

    if parsed.action == "create-card":
        if target.exists():
            target_identity = (
                transaction.memory_file_identity(target) if transaction else None
            )
            existing = target.read_text(encoding="utf-8")
            if (
                transaction
                and transaction.memory_file_identity(target) != target_identity
            ):
                raise TransactionRecoveryError(
                    f"memory file {target.name} changed while it was read"
                )
            if existing.strip() == parsed.content.strip():
                msg = f"{src.name}: create-card -> {target.name} already identical, move to processed"
                if apply:
                    if transaction is None or target_identity is None:
                        raise TransactionRecoveryError(
                            "identical card apply requires an active transaction"
                        )
                    transaction.watch_memory_file(target, target_identity)
                    move_processed()
                return msg, True
            if not force:
                return (f"{src.name}: SKIP - {target.name} exists with different content (use --force)", False)
            msg = f"{src.name}: create-card -> {target.name} (FORCE overwrite)"
            if apply:
                payload = parsed.content if parsed.content.endswith("\n") else parsed.content + "\n"
                move_processed()
                write_target(target, payload, target_identity)
                touched.append((target, f"create-card (force) from {src.name}"))
            return msg, True
        msg = f"{src.name}: create-card -> {target.name}"
        if apply:
            payload = parsed.content if parsed.content.endswith("\n") else parsed.content + "\n"
            move_processed()
            write_target(target, payload, None)
            touched.append((target, f"create-card from {src.name}"))
        return msg, True

    if parsed.action == "update-card":
        if not target.exists():
            return (f"{src.name}: ERROR - update-card target {target.name} does not exist", False)
        msg = f"{src.name}: update-card -> {target.name} (append)"
        if apply:
            target_identity = (
                transaction.memory_file_identity(target) if transaction else None
            )
            existing = target.read_text(encoding="utf-8")
            if (
                transaction
                and transaction.memory_file_identity(target) != target_identity
            ):
                raise TransactionRecoveryError(
                    f"memory file {target.name} changed while it was read"
                )
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            move_processed()
            write_target(
                target,
                existing + sep + parsed.content + "\n",
                target_identity,
            )
            touched.append((target, f"update-card append from {src.name}"))
        return msg, True

    return (f"{src.name}: unknown action {parsed.action!r}", False)


def _preflight_for_commit(
    memory_dir: Path,
    planned_targets: list[Path],
    commit_author: str | None,
) -> int:
    """Run the three pre-flight checks from the spec. 0 = ok, 2 = abort.

    Called BEFORE any file write when --commit is set, so a failure leaves
    the on-disk state untouched.
    """
    author_error = validate_author_format(commit_author)
    if author_error:
        print(
            f"memory-doctor: invalid --commit-author: {author_error}\n"
            f"  fix: use `--commit-author \"Name <email>\"`",
            file=sys.stderr,
        )
        return 2

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

    try:
        dirty = files_have_uncommitted_changes(memory_dir, planned_targets)
    except GitStatusError as exc:
        print(
            f"memory-doctor: refusing to commit, git status failed:\n  {exc}",
            file=sys.stderr,
        )
        return 2
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
            resolve_card_target(memory_dir, parsed.target)
        except UnsafeTargetError:
            continue
        targets.append(
            memory_dir.resolve() / parsed.target.removeprefix("cards/")
        )
    return targets


def _apply_preflight(
    cfg: PathConfig,
    pending: list[Path],
    *,
    commit: bool,
    commit_author: str | None,
) -> int:
    """Validate an apply without creating transaction state or mutating files."""
    try:
        for path in pending:
            preflight_managed_artifact(
                path, label="pending handoff", required=True
            )
        preflight_visible_path_aliases(pending, label="pending handoff")
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor ingest: refusing unsafe transaction artifact: {exc}",
            file=sys.stderr,
        )
        return 2

    planned = _plan_targets(pending, cfg.memory_dir)
    try:
        for path in planned:
            preflight_managed_artifact(path, label="card target")
        preflight_visible_path_aliases(planned, label="card target")
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor ingest: refusing unsafe transaction artifact: {exc}",
            file=sys.stderr,
        )
        return 2

    collisions = [
        path.name
        for path in pending
        if (
            (cfg.handoffs_dir / "processed" / path.name).exists()
            or (cfg.handoffs_dir / "processed" / path.name).is_symlink()
        )
    ]
    if collisions:
        print(
            "memory-doctor ingest: refusing to apply, processed handoff already exists: "
            + ", ".join(collisions),
            file=sys.stderr,
        )
        return 2

    move_paths = [path.resolve(strict=False) for path in pending]
    move_paths.extend(
        (cfg.handoffs_dir / "processed" / path.name).resolve(strict=False)
        for path in pending
    )
    try:
        preflight_visible_path_aliases(
            [*planned, *move_paths], label="transaction visible"
        )
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor ingest: refusing unsafe transaction artifact: {exc}",
            file=sys.stderr,
        )
        return 2
    visible_overlaps = sorted(
        {path.resolve(strict=False) for path in planned} & set(move_paths),
        key=str,
    )
    if visible_overlaps:
        print(
            "memory-doctor ingest: refusing to apply, a card target overlaps "
            "a pending or processed handoff path: "
            + ", ".join(path.name for path in visible_overlaps),
            file=sys.stderr,
        )
        return 2
    if commit:
        return _preflight_for_commit(cfg.memory_dir, planned, commit_author)

    if is_git_repo(cfg.memory_dir):
        try:
            dirty = files_have_uncommitted_changes(cfg.memory_dir, planned)
        except GitStatusError as exc:
            print(
                f"memory-doctor: refusing to apply, git status failed:\n  {exc}",
                file=sys.stderr,
            )
            return 2
        if dirty:
            print(
                "memory-doctor: refusing to apply, target files have uncommitted local changes:",
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


def _run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    force: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
    transaction: ApplyTransaction | None = None,
) -> int:
    if commit and not apply:
        # Friendlier than erroring: people experimenting with the flag often
        # forget --apply, and the message guides them to the right thing.
        print("memory-doctor ingest: skipping commit (dry-run; use --apply)")

    pending = iter_pending_handoffs(cfg.handoffs_dir)
    if not pending:
        print("memory-doctor ingest: no pending handoffs")
        return 0

    if apply:
        # The first pass happens before transaction construction. This second
        # pass closes the race between the static preflight and the first
        # mutation while the caller holds the apply lock.
        rc = _apply_preflight(
            cfg,
            pending,
            commit=commit,
            commit_author=commit_author,
        )
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
            source_identity = (
                transaction.handoff_identity(p) if transaction else None
            )
            parsed = parse_handoff(p)
            if (
                transaction
                and transaction.handoff_identity(p) != source_identity
            ):
                raise TransactionRecoveryError(
                    f"handoff {p.name} changed while it was parsed"
                )
        except HandoffParseError as e:
            print(f"  {p.name}: PARSE ERROR - {e}")
            all_ok = False
            skipped += 1
            continue
        msg, ok = _process_handoff(
            parsed,
            cfg.memory_dir,
            cfg.handoffs_dir,
            apply=apply,
            force=force,
            touched=touched,
            transaction=transaction,
            source_identity=source_identity,
        )
        print(f"  {msg}")
        if ok:
            promoted += 1
        else:
            skipped += 1
            all_ok = False

    if apply and transaction:
        transaction.commit()

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
            print(
                f"\nerror: commit failed ({result.error_kind}): {result.error_message}\n"
                "  file changes are preserved; review `git status` and commit them manually",
                file=sys.stderr,
            )
            return 1
    elif apply and commit and not touched:
        print("\nno changes to commit")

    return 0 if all_ok else 1


def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    force: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    if not apply:
        return _run(
            cfg,
            apply=False,
            force=force,
            commit=commit,
            commit_author=commit_author,
        )

    try:
        recovery_pending = has_pending_transaction_recovery(cfg.memory_dir)
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor ingest: transaction recovery incomplete: {exc}",
            file=sys.stderr,
        )
        return 2

    pending = iter_pending_handoffs(cfg.handoffs_dir)
    if not pending and not recovery_pending:
        try:
            recovery_pending = has_pending_transaction_recovery(cfg.memory_dir)
        except TransactionRecoveryError as exc:
            print(
                f"memory-doctor ingest: transaction recovery incomplete: {exc}",
                file=sys.stderr,
            )
            return 2
        if not recovery_pending:
            print("memory-doctor ingest: no pending handoffs")
            return 0

    if not recovery_pending:
        rc = _apply_preflight(
            cfg,
            pending,
            commit=commit,
            commit_author=commit_author,
        )
        if rc != 0:
            return rc

    try:
        preflight_transaction_capabilities(cfg.memory_dir, cfg.handoffs_dir)
    except (OSError, RuntimeError, TransactionRecoveryError) as exc:
        print(
            f"memory-doctor ingest: transaction recovery incomplete: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        (cfg.handoffs_dir / "processed").mkdir(exist_ok=True)
        transaction = ApplyTransaction(cfg.memory_dir, cfg.handoffs_dir)
    except (OSError, RuntimeError, TransactionRecoveryError) as exc:
        print(
            f"memory-doctor ingest: transaction recovery incomplete: {exc}",
            file=sys.stderr,
        )
        return 2

    entered = False
    try:
        with transaction:
            entered = True
            transaction.preflight_mutations()
            if transaction.recovered:
                print(
                    "memory-doctor ingest: recovered an interrupted apply transaction",
                    file=sys.stderr,
                )
            return _run(
                cfg,
                apply=True,
                force=force,
                commit=commit,
                commit_author=commit_author,
                transaction=transaction,
            )
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor ingest: transaction recovery incomplete: {exc}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        if not entered:
            print(
                f"memory-doctor ingest: transaction recovery incomplete: {exc}",
                file=sys.stderr,
            )
            return 2
        outcome = "changes preserved" if transaction.committed else "changes rolled back"
        print(
            f"memory-doctor ingest: apply failed ({outcome}): {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        if not entered:
            print(
                f"memory-doctor ingest: transaction recovery incomplete: {exc}",
                file=sys.stderr,
            )
            return 2
        outcome = "changes preserved" if transaction.committed else "changes rolled back"
        print(
            f"memory-doctor ingest: apply failed ({outcome}): {exc}",
            file=sys.stderr,
        )
        return 1
