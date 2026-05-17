"""Ingest verb: promote pending handoffs into cards."""
from __future__ import annotations

import shutil
from pathlib import Path

from memory_doctor.parsing import HandoffParseError, ParsedHandoff, parse_handoff
from memory_doctor.paths import PathConfig
from memory_doctor.safety import UnsafeTargetError, resolve_card_target


def _process_handoff(
    parsed: ParsedHandoff,
    memory_dir: Path,
    handoffs_dir: Path,
    *,
    apply: bool,
    force: bool,
) -> tuple[str, bool]:
    """Returns (message, success). On dry-run, reports without writing."""
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
                target.write_text(parsed.content if parsed.content.endswith("\n") else parsed.content + "\n")
                shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
            return msg, True
        msg = f"{src.name}: create-card -> {target.name}"
        if apply:
            target.write_text(parsed.content if parsed.content.endswith("\n") else parsed.content + "\n")
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    if parsed.action == "update-card":
        if not target.exists():
            return (f"{src.name}: ERROR - update-card target {target.name} does not exist", False)
        msg = f"{src.name}: update-card -> {target.name} (append)"
        if apply:
            existing = target.read_text()
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            target.write_text(existing + sep + parsed.content + "\n")
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    return (f"{src.name}: unknown action {parsed.action!r}", False)


def run(cfg: PathConfig, *, apply: bool = False, force: bool = False) -> int:
    pending = sorted(p for p in cfg.handoffs_dir.glob("*.md"))
    if not pending:
        print("memory-doctor ingest: no pending handoffs")
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor ingest ({mode}): {len(pending)} handoff(s)")
    all_ok = True
    for p in pending:
        try:
            parsed = parse_handoff(p)
        except HandoffParseError as e:
            print(f"  {p.name}: PARSE ERROR - {e}")
            all_ok = False
            continue
        msg, ok = _process_handoff(parsed, cfg.memory_dir, cfg.handoffs_dir, apply=apply, force=force)
        print(f"  {msg}")
        if not ok:
            all_ok = False

    return 0 if all_ok else 1
