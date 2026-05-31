"""Compact verb: flatten multi-line MEMORY.md entries into topic files."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from memory_doctor.paths import PathConfig
from memory_doctor.safety import (
    UnsafeTargetError,
    atomic_write_text,
    resolve_card_target,
)


BULLET_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)\s*(.*)$")
INDENTED_CONTINUATION_RE = re.compile(r"^\s{2,}\S")


def _flatten_marker(today: str, flatten: "Flatten") -> str:
    """Stable marker so re-applying the same flatten plan is idempotent.

    Hashes the FULL payload (target + title + bullet hook + detail lines) so
    two entries with the same title but different detail don't collide and
    silently swallow each other's content.
    """
    payload = "|".join([
        flatten.target_name,
        flatten.title,
        flatten.bullet_text.strip(),
        "\n".join(flatten.detail_lines),
    ])
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:{today}:{h} -->"


@dataclass(frozen=True)
class Flatten:
    line_index: int           # index of the bullet line in MEMORY.md
    title: str
    target_name: str          # filename, e.g. 'topic-b.md'
    bullet_text: str          # the bullet's original hook text (kept in the index)
    detail_lines: list[str]   # the continuation lines (flattened into the topic file)


@dataclass(frozen=True)
class CompactionPlan:
    original_lines: int
    flattens: list[Flatten]
    missing_targets: list[str]
    projected_lines: int
    unsafe_targets: list[str] = field(default_factory=list)


def plan_compaction(memory_dir: Path, max_lines: int) -> CompactionPlan:
    index_path = memory_dir / "MEMORY.md"
    lines = index_path.read_text().splitlines() if index_path.exists() else []
    flattens: list[Flatten] = []
    missing: list[str] = []
    unsafe: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = BULLET_RE.match(line)
        if not m:
            i += 1
            continue
        title = m.group(1)
        target_url = m.group(2)
        bullet_text = m.group(3)
        target_name = target_url.removeprefix("cards/")
        details: list[str] = []
        j = i + 1
        while j < len(lines) and INDENTED_CONTINUATION_RE.match(lines[j]):
            details.append(lines[j].strip())
            j += 1
        if details:
            # Reject targets that would escape memory_dir or are otherwise unsafe;
            # exclude them from the flatten plan entirely so we never write outside.
            try:
                resolve_card_target(memory_dir, target_name)
            except UnsafeTargetError:
                unsafe.append(target_name)
                i = j
                continue
            flattens.append(Flatten(
                line_index=i,
                title=title,
                target_name=target_name,
                bullet_text=bullet_text,
                detail_lines=details,
            ))
            if not (memory_dir / target_name).exists():
                missing.append(target_name)
        i = j

    projected = len(lines) - sum(len(f.detail_lines) for f in flattens)
    return CompactionPlan(
        original_lines=len(lines),
        flattens=flattens,
        missing_targets=missing,
        projected_lines=projected,
        unsafe_targets=unsafe,
    )


def _apply_flatten(memory_dir: Path, plan: CompactionPlan) -> None:
    index_path = memory_dir / "MEMORY.md"
    lines = index_path.read_text().splitlines()
    today = dt.date.today().isoformat()

    applied: list[Flatten] = []
    for flatten in plan.flattens:
        # Defense-in-depth: re-validate target on apply; plan_compaction already
        # filters unsafe entries, but never trust a cached plan with raw paths.
        try:
            target_path = resolve_card_target(memory_dir, flatten.target_name)
        except UnsafeTargetError:
            continue
        existing = target_path.read_text()
        marker = _flatten_marker(today, flatten)
        if marker in existing:
            # Already applied for this title/date - skip the topic append but
            # still drop the detail lines from the index below for consistency.
            applied.append(flatten)
            continue
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        appended = (
            f"{existing}{sep}{marker}\n## From index ({today})\n\n"
            f"{flatten.bullet_text.strip()}\n\n"
            + "\n".join(flatten.detail_lines)
            + "\n"
        )
        atomic_write_text(target_path, appended)
        applied.append(flatten)

    keep: list[str] = []
    skip_indexes: set[int] = set()
    for flatten in applied:
        for off in range(1, len(flatten.detail_lines) + 1):
            skip_indexes.add(flatten.line_index + off)
    for idx, line in enumerate(lines):
        if idx in skip_indexes:
            continue
        keep.append(line)
    atomic_write_text(index_path, "\n".join(keep) + ("\n" if keep else ""))


def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    import sys
    from memory_doctor.git import (
        commit_run,
        files_have_uncommitted_changes,
        is_git_repo,
        validate_author_format,
        working_tree_sane,
    )

    if commit and not apply:
        print("memory-doctor compact: skipping commit (dry-run; use --apply)")

    index_path = cfg.memory_dir / "MEMORY.md"
    if not index_path.exists():
        print(f"memory-doctor compact: {index_path} does not exist")
        return 0

    plan = plan_compaction(cfg.memory_dir, cfg.max_lines)
    if plan.original_lines <= cfg.max_lines:
        print(f"memory-doctor compact: {plan.original_lines} lines <= {cfg.max_lines}, no action needed")
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor compact ({mode}): MEMORY.md {plan.original_lines} -> ~{plan.projected_lines} lines")

    if plan.unsafe_targets:
        print("\nWARNING: skipping entries with unsafe targets (path traversal / escapes memory dir):")
        for t in plan.unsafe_targets:
            print(f"  - {t}")

    if plan.missing_targets:
        print("\nERROR: target topic files missing for some flatten candidates:")
        for t in plan.missing_targets:
            print(f"  - {t}")
        print("\nRefusing to compact: would orphan content. Create the missing card(s) first.")
        return 2

    if not plan.flattens:
        print("\nNo multi-line entries to flatten. Manual archival of older sections is required.")
        return 0

    print("\nFlatten candidates:")
    for f in plan.flattens:
        print(f"  [{f.title}] -> {f.target_name} (+{len(f.detail_lines)} line(s))")

    if plan.projected_lines > cfg.max_lines:
        print(f"\nWARNING: even after flattening, MEMORY.md would be {plan.projected_lines} lines (still over {cfg.max_lines}).")
        print("Manual archival of older entries is required.")

    if not apply:
        return 0

    if commit:
        author_error = validate_author_format(commit_author)
        if author_error:
            print(
                f"memory-doctor: invalid --commit-author: {author_error}\n"
                f"  fix: use `--commit-author \"Name <email>\"`",
                file=sys.stderr,
            )
            return 2
        if not is_git_repo(cfg.memory_dir):
            print(
                f"memory-doctor: --commit requires the memory dir to be a git repo\n"
                f"  memory dir: {cfg.memory_dir}\n"
                f"  fix: run `memory-doctor init-git` once, then retry",
                file=sys.stderr,
            )
            return 2
        ok, reason = working_tree_sane(cfg.memory_dir)
        if not ok:
            print(
                f"memory-doctor: refusing to commit, git is in the middle of a {reason}\n"
                f"  fix: complete or abort the in-progress operation, then retry",
                file=sys.stderr,
            )
            return 2
        planned = [cfg.memory_dir / f.target_name for f in plan.flattens] + [index_path]
        dirty = files_have_uncommitted_changes(cfg.memory_dir, planned)
        if dirty:
            print(
                "memory-doctor: refusing to commit, target files have uncommitted local changes:",
                file=sys.stderr,
            )
            for path, status in dirty:
                print(f"  - {path.name} ({status})", file=sys.stderr)
            print("  fix: review with `git diff`, commit/stash/discard, then retry", file=sys.stderr)
            return 2

    _apply_flatten(cfg.memory_dir, plan)
    print(f"\nApplied. MEMORY.md now {plan.projected_lines} lines.")

    if not commit:
        return 0

    files = [cfg.memory_dir / f.target_name for f in plan.flattens] + [index_path]
    subject = (
        f"memory-doctor compact: {len(plan.flattens)} entr"
        f"{'ies' if len(plan.flattens) != 1 else 'y'} flattened, "
        f"MEMORY.md {plan.original_lines} -> {plan.projected_lines} lines"
    )
    body_lines = [
        f"- {f.target_name} (appended {len(f.detail_lines)}-line detail block from index)"
        for f in plan.flattens
    ]
    delta = plan.original_lines - plan.projected_lines
    body_lines.append(
        f"- MEMORY.md ({len(plan.flattens)} entries flattened to one-liners, -{delta} lines)"
    )
    body = "\n".join(body_lines)
    result = commit_run(
        memory_dir=cfg.memory_dir,
        files=files,
        subject=subject,
        body=body,
        author=commit_author,
    )
    if result.error_kind is None:
        print(f"\nCommitted {result.sha}")
        return 0
    if result.error_kind == "hook":
        print(
            "\nerror: pre-commit hook rejected the commit; your file changes are staged but not committed",
            file=sys.stderr,
        )
        print(f"  files: {', '.join(f.name for f in files)}", file=sys.stderr)
        print(f"  details: {result.error_message}", file=sys.stderr)
        return 1
    print(f"\nerror: commit failed ({result.error_kind}): {result.error_message}", file=sys.stderr)
    return 1
