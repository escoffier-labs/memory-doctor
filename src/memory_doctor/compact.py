"""Compact verb: flatten multi-line MEMORY.md entries into topic files."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from memory_doctor.paths import PathConfig
from memory_doctor.safety import UnsafeTargetError, resolve_card_target


BULLET_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)\s*(.*)$")
INDENTED_CONTINUATION_RE = re.compile(r"^\s{2,}\S")


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

    for flatten in plan.flattens:
        # Defense-in-depth: re-validate target on apply; plan_compaction already
        # filters unsafe entries, but never trust a cached plan with raw paths.
        try:
            target_path = resolve_card_target(memory_dir, flatten.target_name)
        except UnsafeTargetError:
            continue
        existing = target_path.read_text()
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        appended = (
            f"{existing}{sep}## From index ({today})\n\n"
            f"{flatten.bullet_text.strip()}\n\n"
            + "\n".join(flatten.detail_lines)
            + "\n"
        )
        target_path.write_text(appended)

    keep: list[str] = []
    skip_indexes: set[int] = set()
    for flatten in plan.flattens:
        for off in range(1, len(flatten.detail_lines) + 1):
            skip_indexes.add(flatten.line_index + off)
    for idx, line in enumerate(lines):
        if idx in skip_indexes:
            continue
        keep.append(line)
    index_path.write_text("\n".join(keep) + ("\n" if keep else ""))


def run(cfg: PathConfig, *, apply: bool = False) -> int:
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

    if apply:
        _apply_flatten(cfg.memory_dir, plan)
        print(f"\nApplied. MEMORY.md now {plan.projected_lines} lines.")
    return 0
