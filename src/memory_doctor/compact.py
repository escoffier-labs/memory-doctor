"""Compact verb: flatten multi-line MEMORY.md entries into topic files."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from memory_doctor.paths import PathConfig, require_unified_layout
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


BULLET_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)\s*(.*)$")
INDENTED_CONTINUATION_RE = re.compile(r"^\s{2,}\S")

# Unicode punctuation that violates the no-em-dash rule (and other ASCII-only
# conventions) mapped to safe replacements. Applied to every line we rewrite
# plus a final whole-file pass on apply. We deliberately do NOT touch the link
# TARGET (the URL inside the parentheses); only the visible text is normalized.
UNICODE_NORMALIZATION = {
    "—": "-",   # em dash
    "–": "-",   # en dash
    "―": "-",   # horizontal bar
    "→": "->",  # right arrow
    "≥": ">=",  # greater-than-or-equal
    "≤": "<=",  # less-than-or-equal
    "≈": "~",   # almost equal
    "·": "-",   # middle dot
}


def _normalize_unicode(text: str) -> str:
    """Replace em/en dashes and a few other unicode glyphs with ASCII."""
    for src, dst in UNICODE_NORMALIZATION.items():
        text = text.replace(src, dst)
    return text


def _has_normalizable(text: str) -> bool:
    """True if `text` contains any glyph the normalizer would rewrite."""
    return any(src in text for src in UNICODE_NORMALIZATION)


def _normalize_index_line(line: str) -> str:
    """Normalize unicode in the VISIBLE parts of an index line only.

    Bullet lines keep their link target byte-for-byte (the documented
    contract above); every other line is normalized whole.
    """
    m = BULLET_RE.match(line)
    if not m:
        return _normalize_unicode(line)
    rebuilt = (
        f"- [{_normalize_unicode(m.group(1))}]({m.group(2)}) "
        f"{_normalize_unicode(m.group(3))}"
    )
    return rebuilt.rstrip()


def _index_has_normalizable(text: str) -> bool:
    """True if any VISIBLE index text would be rewritten by the normalizer.

    Mirrors _normalize_index_line: unicode inside a bullet's link target does
    not count, so compact never reports work it would refuse to do.
    """
    for line in text.splitlines():
        m = BULLET_RE.match(line)
        if m:
            if _has_normalizable(m.group(1)) or _has_normalizable(m.group(3)):
                return True
        elif _has_normalizable(line):
            return True
    return False


def _flatten_marker(today: str, flatten: "Flatten") -> str:
    """Stable marker so re-applying the same flatten plan is idempotent.

    Hashes the FULL payload (target + title + bullet hook + detail lines) so
    two entries with the same title but different detail don't collide and
    silently swallow each other's content.
    """
    payload = json.dumps([
        flatten.target_name,
        flatten.title,
        flatten.bullet_text.strip(),
        "\n".join(flatten.detail_lines),
    ], ensure_ascii=False, separators=(",", ":"))
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:{today}:{h} -->"


def _legacy_flatten_marker(today: str, flatten: "Flatten") -> str:
    payload = "|".join([
        flatten.target_name,
        flatten.title,
        flatten.bullet_text.strip(),
        "\n".join(flatten.detail_lines),
    ])
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:{today}:{h} -->"


def _tighten_marker(today: str, tighten: "Tighten") -> str:
    """Stable marker so re-applying the same tighten plan is idempotent.

    Hashes the FULL original hook + target + title so two entries that share a
    title but differ in hook text don't collide. Uses a distinct `tighten:`
    prefix so it never aliases a flatten marker.
    """
    payload = json.dumps([
        tighten.target_name,
        tighten.title,
        tighten.full_hook.strip(),
    ], ensure_ascii=False, separators=(",", ":"))
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:tighten:{today}:{h} -->"


def _legacy_tighten_marker(today: str, tighten: "Tighten") -> str:
    payload = "|".join([
        tighten.target_name,
        tighten.title,
        tighten.full_hook.strip(),
    ])
    h = hashlib.sha256(payload.encode()).hexdigest()[:12]
    return f"<!-- compact:tighten:{today}:{h} -->"


def _flatten_preserved_block(
    today: str, flatten: "Flatten", *, marker: str | None = None
) -> str:
    return (
        f"{marker or _flatten_marker(today, flatten)}\n"
        f"## From index ({today})\n\n"
        f"{flatten.bullet_text.strip()}\n\n"
        + "\n".join(flatten.detail_lines)
        + "\n"
    )


def _tighten_preserved_block(
    today: str, tighten: "Tighten", *, marker: str | None = None
) -> str:
    return (
        f"{marker or _tighten_marker(today, tighten)}\n"
        f"## From index ({today})\n\n"
        f"{tighten.full_hook.strip()}\n"
    )


@dataclass(frozen=True)
class Flatten:
    line_index: int           # index of the bullet line in MEMORY.md
    title: str
    target_name: str          # filename, e.g. 'topic-b.md'
    bullet_text: str          # the bullet's original hook text (kept in the index)
    detail_lines: list[str]   # the continuation lines (flattened into the topic file)


@dataclass(frozen=True)
class Tighten:
    line_index: int           # index of the bullet line in MEMORY.md
    title: str
    target_name: str          # filename, e.g. 'topic-b.md'
    full_hook: str            # the entry's original (overlong) hook text
    short_hook: str           # the truncated hook that replaces it in the index


@dataclass(frozen=True)
class CompactionPlan:
    original_lines: int
    flattens: list[Flatten]
    missing_targets: list[str]
    projected_lines: int
    tightens: list[Tighten] = field(default_factory=list)
    projected_bytes: int = 0
    original_bytes: int = 0
    unsafe_targets: list[str] = field(default_factory=list)
    target_identities: dict[str, object] = field(default_factory=dict)


def _truncate_hook(hook: str, max_hook_chars: int) -> str:
    """Truncate `hook` to <= max_hook_chars at a word boundary, append '...'.

    The ellipsis itself counts toward the budget so the rewritten visible text
    never exceeds the limit. If even the first word overflows, hard-truncate.
    """
    hook = hook.strip()
    if len(hook) <= max_hook_chars:
        return hook
    budget = max(0, max_hook_chars - 3)  # room for the trailing '...'
    cut = hook[:budget]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "..."


def plan_compaction(
    memory_dir: Path,
    max_lines: int,
    *,
    max_hook_chars: int = 140,
    transaction: ApplyTransaction | None = None,
) -> CompactionPlan:
    index_path = memory_dir / "MEMORY.md"
    raw = index_path.read_bytes() if index_path.exists() else b""
    # Strict decode: raises UnicodeDecodeError on invalid UTF-8. A lossy
    # errors="replace" decode here would let apply persist U+FFFD bytes and
    # permanently corrupt the index; run() catches this and aborts instead.
    lines = raw.decode("utf-8").splitlines() if raw else []
    flattens: list[Flatten] = []
    tightens: list[Tighten] = []
    missing: list[str] = []
    unsafe: list[str] = []
    target_identities: dict[str, object] = {}

    def record_target_identity(target_name: str, target_path: Path) -> bool:
        if transaction is None:
            return target_path.exists()
        identity = transaction.memory_file_identity(target_path)
        if identity is None:
            return False
        previous = target_identities.get(target_name)
        if previous is not None and previous != identity:
            raise TransactionRecoveryError(
                f"memory file {target_name} changed while compaction was planned"
            )
        target_identities[target_name] = identity
        return True

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
                resolve_card_target(memory_dir, target_url)
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
            if not record_target_identity(target_name, memory_dir / target_name):
                missing.append(target_name)
            i = j
            continue
        # Single-line entry: a "tighten" candidate when the hook is overlong AND
        # the linked card actually exists (a dangling link may be the only record
        # of this hook, so we never truncate those).
        hook = bullet_text.strip()
        if len(_normalize_unicode(hook)) > max_hook_chars:
            try:
                resolved = resolve_card_target(memory_dir, target_url)
            except UnsafeTargetError:
                unsafe.append(target_name)
                i = j
                continue
            if record_target_identity(target_name, resolved):
                normalized = _normalize_unicode(hook)
                tightens.append(Tighten(
                    line_index=i,
                    title=title,
                    target_name=target_name,
                    full_hook=normalized,
                    short_hook=_truncate_hook(normalized, max_hook_chars),
                ))
        i = j

    projected_lines = len(lines) - sum(len(f.detail_lines) for f in flattens)
    projected_bytes = _projected_index_bytes(lines, flattens, tightens, max_hook_chars)
    return CompactionPlan(
        original_lines=len(lines),
        flattens=flattens,
        missing_targets=missing,
        projected_lines=projected_lines,
        tightens=tightens,
        projected_bytes=projected_bytes,
        original_bytes=len(raw),
        unsafe_targets=unsafe,
        target_identities=target_identities,
    )


def _rewrite_index_lines(
    lines: list[str],
    flattens: list[Flatten],
    tightens: list[Tighten],
    max_hook_chars: int,
) -> list[str]:
    """Pure projection of MEMORY.md after applying flattens + tightens.

    Used for byte estimation in planning and for the actual rewrite on apply,
    so the projected byte count and the real result stay in lockstep.
    """
    skip_indexes: set[int] = set()
    for flatten in flattens:
        for off in range(1, len(flatten.detail_lines) + 1):
            skip_indexes.add(flatten.line_index + off)
    tighten_by_index = {t.line_index: t for t in tightens}
    out: list[str] = []
    for idx, line in enumerate(lines):
        if idx in skip_indexes:
            continue
        if idx in tighten_by_index:
            t = tighten_by_index[idx]
            m = BULLET_RE.match(line)
            if m:
                prefix = f"- [{_normalize_unicode(m.group(1))}]({m.group(2)}) "
                out.append(prefix + t.short_hook)
                continue
        out.append(_normalize_index_line(line))
    return out


def _projected_index_bytes(
    lines: list[str],
    flattens: list[Flatten],
    tightens: list[Tighten],
    max_hook_chars: int,
) -> int:
    rewritten = _rewrite_index_lines(lines, flattens, tightens, max_hook_chars)
    text = "\n".join(rewritten) + ("\n" if rewritten else "")
    return len(text.encode("utf-8"))


def _apply_flatten(
    memory_dir: Path,
    plan: CompactionPlan,
    transaction: ApplyTransaction | None = None,
    index_identity=None,
) -> None:
    index_path = memory_dir / "MEMORY.md"
    if transaction and index_identity is None:
        index_identity = transaction.memory_file_identity(index_path)
    lines = index_path.read_bytes().decode("utf-8").splitlines()
    if transaction and transaction.memory_file_identity(index_path) != index_identity:
        raise TransactionRecoveryError(
            "memory index changed while the compaction plan was read"
        )
    today = dt.date.today().isoformat()
    current_target_identities = dict(plan.target_identities)

    def write_target(path: Path, content: str, expected_identity=None) -> None:
        if transaction:
            transaction.write_text(
                path,
                content,
                expected_identity=expected_identity,
            )
        else:
            atomic_write_text(path, content)

    applied: list[Flatten] = []
    for flatten in plan.flattens:
        # Defense-in-depth: re-validate target on apply; plan_compaction already
        # filters unsafe entries, but never trust a cached plan with raw paths.
        try:
            target_path = resolve_card_target(memory_dir, flatten.target_name)
        except UnsafeTargetError:
            continue
        target_identity = current_target_identities.get(flatten.target_name)
        if transaction and target_identity is None:
            target_identity = transaction.memory_file_identity(target_path)
            current_target_identities[flatten.target_name] = target_identity
        if (
            transaction
            and transaction.memory_file_identity(target_path) != target_identity
        ):
            raise TransactionRecoveryError(
                f"memory file {target_path.name} changed after compaction was planned"
            )
        existing = target_path.read_text(encoding="utf-8")
        if (
            transaction
            and transaction.memory_file_identity(target_path) != target_identity
        ):
            raise TransactionRecoveryError(
                f"memory file {target_path.name} changed while it was read"
            )
        marker = _flatten_marker(today, flatten)
        if marker in existing:
            if _flatten_preserved_block(today, flatten) not in existing:
                raise TransactionRecoveryError(
                    f"memory file {target_path.name} contains compact marker "
                    "without its preserved payload"
                )
            # Already applied for this title/date - skip the topic append but
            # still drop the detail lines from the index below for consistency.
            applied.append(flatten)
            continue
        legacy_marker = _legacy_flatten_marker(today, flatten)
        if legacy_marker in existing:
            if _flatten_preserved_block(
                today,
                flatten,
                marker=legacy_marker,
            ) not in existing:
                raise TransactionRecoveryError(
                    f"memory file {target_path.name} contains legacy compact "
                    "marker without its preserved payload"
                )
            applied.append(flatten)
            continue
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        appended = f"{existing}{sep}{_flatten_preserved_block(today, flatten)}"
        write_target(target_path, appended, target_identity)
        if transaction:
            current_target_identities[flatten.target_name] = (
                transaction.memory_file_identity(target_path)
            )
        applied.append(flatten)

    applied_tightens: list[Tighten] = []
    for tighten in plan.tightens:
        # Mirror _apply_flatten: re-validate, append FULL hook under an
        # idempotent marker, then let the index rewrite swap in the short hook.
        try:
            target_path = resolve_card_target(memory_dir, tighten.target_name)
        except UnsafeTargetError:
            continue
        if not target_path.exists():
            # Dangling link: index may be the only record. Do not move; the
            # whole-file normalization pass below still scrubs unicode in place.
            continue
        target_identity = current_target_identities.get(tighten.target_name)
        if transaction and target_identity is None:
            target_identity = transaction.memory_file_identity(target_path)
            current_target_identities[tighten.target_name] = target_identity
        if (
            transaction
            and transaction.memory_file_identity(target_path) != target_identity
        ):
            raise TransactionRecoveryError(
                f"memory file {target_path.name} changed after compaction was planned"
            )
        existing = target_path.read_text(encoding="utf-8")
        if (
            transaction
            and transaction.memory_file_identity(target_path) != target_identity
        ):
            raise TransactionRecoveryError(
                f"memory file {target_path.name} changed while it was read"
            )
        marker = _tighten_marker(today, tighten)
        if marker in existing:
            if _tighten_preserved_block(today, tighten) not in existing:
                raise TransactionRecoveryError(
                    f"memory file {target_path.name} contains compact marker "
                    "without its preserved payload"
                )
        else:
            legacy_marker = _legacy_tighten_marker(today, tighten)
            if legacy_marker in existing:
                if _tighten_preserved_block(
                    today,
                    tighten,
                    marker=legacy_marker,
                ) not in existing:
                    raise TransactionRecoveryError(
                        f"memory file {target_path.name} contains legacy compact "
                        "marker without its preserved payload"
                    )
            else:
                sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
                appended = f"{existing}{sep}{_tighten_preserved_block(today, tighten)}"
                write_target(target_path, appended, target_identity)
                if transaction:
                    current_target_identities[tighten.target_name] = (
                        transaction.memory_file_identity(target_path)
                    )
        applied_tightens.append(tighten)

    if transaction:
        for target_name, expected_identity in current_target_identities.items():
            target_path = resolve_card_target(memory_dir, target_name)
            if transaction.memory_file_identity(target_path) != expected_identity:
                raise TransactionRecoveryError(
                    f"memory file {target_path.name} changed before the index rewrite"
                )
            transaction.watch_memory_file(target_path, expected_identity)

    rewritten = _rewrite_index_lines(lines, applied, applied_tightens, 0)
    write_target(
        index_path,
        "\n".join(rewritten) + ("\n" if rewritten else ""),
        index_identity,
    )


def _apply_preflight(
    cfg: PathConfig,
    plan: CompactionPlan,
    index_path: Path,
    *,
    commit: bool,
    commit_author: str | None,
) -> int:
    """Validate an apply without creating transaction state or writing files."""
    from memory_doctor.git import (
        GitStatusError,
        files_have_uncommitted_changes,
        is_git_repo,
        validate_author_format,
        working_tree_sane,
    )

    card_targets = [cfg.memory_dir / item.target_name for item in plan.flattens]
    card_targets += [cfg.memory_dir / item.target_name for item in plan.tightens]
    try:
        preflight_managed_artifact(index_path, label="memory index", required=True)
        for path in card_targets:
            preflight_managed_artifact(path, label="compact target")
        preflight_visible_path_aliases(
            [index_path, *card_targets], label="compact target"
        )
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor compact: refusing unsafe transaction artifact: {exc}",
            file=sys.stderr,
        )
        return 2

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

    if is_git_repo(cfg.memory_dir):
        planned = card_targets + [index_path]
        action = "commit" if commit else "apply"
        try:
            dirty = files_have_uncommitted_changes(cfg.memory_dir, planned)
        except GitStatusError as exc:
            print(
                f"memory-doctor: refusing to {action}, git status failed:\n  {exc}",
                file=sys.stderr,
            )
            return 2
        if dirty:
            print(
                f"memory-doctor: refusing to {action}, target files have uncommitted local changes:",
                file=sys.stderr,
            )
            for path, status in dirty:
                print(f"  - {path.name} ({status})", file=sys.stderr)
            print(
                "  fix: review with `git diff`, commit/stash/discard, then retry",
                file=sys.stderr,
            )
            return 2

    for name in sorted(
        {item.target_name for item in plan.flattens}
        | {item.target_name for item in plan.tightens}
    ):
        card_path = cfg.memory_dir / name
        if not card_path.exists():
            continue
        try:
            card_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            print(
                f"memory-doctor compact: {card_path} is not valid UTF-8 "
                f"(bad byte at offset {exc.start}); refusing to touch it\n"
                f"  fix: repair the file's encoding manually, then retry",
                file=sys.stderr,
            )
            return 2
    return 0


def _run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
    transaction: ApplyTransaction | None = None,
) -> int:
    from memory_doctor.git import commit_run

    if commit and not apply:
        print("memory-doctor compact: skipping commit (dry-run; use --apply)")

    index_path = cfg.memory_dir / "MEMORY.md"
    if not index_path.exists():
        print(f"memory-doctor compact: {index_path} does not exist")
        return 0

    index_identity = (
        transaction.memory_file_identity(index_path) if transaction else None
    )
    try:
        plan = plan_compaction(
            cfg.memory_dir,
            cfg.max_lines,
            max_hook_chars=cfg.max_hook_chars,
            transaction=transaction,
        )
    except UnicodeDecodeError as e:
        print(
            f"memory-doctor compact: {index_path} is not valid UTF-8 "
            f"(bad byte at offset {e.start}); refusing to touch it\n"
            f"  fix: repair the file's encoding manually, then retry",
            file=sys.stderr,
        )
        return 2

    index_text = index_path.read_bytes().decode("utf-8")
    if transaction and transaction.memory_file_identity(index_path) != index_identity:
        raise TransactionRecoveryError(
            "memory index changed while the compaction plan was read"
        )
    has_unicode = _index_has_normalizable(index_text)
    over_lines = plan.original_lines > cfg.max_lines
    over_bytes = plan.original_bytes > cfg.max_bytes
    has_work = bool(plan.flattens) or bool(plan.tightens) or has_unicode

    # Nothing to do only when genuinely nothing: under both thresholds AND no
    # flatten/tighten candidates AND no unicode to scrub.
    if not over_lines and not over_bytes and not has_work:
        print(
            f"memory-doctor compact: {plan.original_lines} lines <= {cfg.max_lines}, "
            f"{plan.original_bytes} bytes <= {cfg.max_bytes}, no action needed"
        )
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(
        f"memory-doctor compact ({mode}): MEMORY.md "
        f"{plan.original_lines} -> ~{plan.projected_lines} lines, "
        f"{plan.original_bytes} -> ~{plan.projected_bytes} bytes"
    )

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

    if plan.flattens:
        print("\nFlatten candidates:")
        for f in plan.flattens:
            print(f"  [{f.title}] -> {f.target_name} (+{len(f.detail_lines)} line(s))")

    if plan.tightens:
        print("\nTighten candidates:")
        for t in plan.tightens:
            print(f"  [{t.title}] -> {t.target_name} ({len(t.full_hook)} -> {len(t.short_hook)} chars)")

    if not plan.flattens and not plan.tightens:
        if has_unicode:
            print("\nNo flatten/tighten candidates; normalizing unicode punctuation only.")
        else:
            print("\nNo multi-line entries to flatten and no overlong hooks to tighten.")
            print("Manual archival of older sections is required.")

    if plan.projected_lines > cfg.max_lines:
        print(f"\nWARNING: even after compacting, MEMORY.md would be {plan.projected_lines} lines (still over {cfg.max_lines}).")
        print("Manual archival of older entries is required.")
    if plan.projected_bytes > cfg.max_bytes:
        print(f"\nWARNING: even after compacting, MEMORY.md would be {plan.projected_bytes} bytes (still over {cfg.max_bytes}).")
        print("Manual archival of older entries is required.")

    if not apply:
        return 0

    rc = _apply_preflight(
        cfg,
        plan,
        index_path,
        commit=commit,
        commit_author=commit_author,
    )
    if rc != 0:
        return rc

    if transaction is None:
        raise TransactionRecoveryError(
            "compact apply requires an active transaction"
        )
    _apply_flatten(
        cfg.memory_dir,
        plan,
        transaction,
        index_identity=index_identity,
    )
    if transaction:
        transaction.commit()
    print(
        f"\nApplied. MEMORY.md now {plan.projected_lines} lines, "
        f"~{plan.projected_bytes} bytes."
    )

    if not commit:
        return 0

    # Dedupe target cards (flatten + tighten may both touch the same card).
    seen: set[str] = set()
    card_files: list[Path] = []
    for name in [f.target_name for f in plan.flattens] + [t.target_name for t in plan.tightens]:
        if name not in seen:
            seen.add(name)
            card_files.append(cfg.memory_dir / name)
    files = card_files + [index_path]
    n_flat = len(plan.flattens)
    n_tight = len(plan.tightens)
    subject = (
        f"memory-doctor compact: {n_flat} flattened, {n_tight} tightened, "
        f"MEMORY.md {plan.original_lines} -> {plan.projected_lines} lines, "
        f"{plan.original_bytes} -> {plan.projected_bytes} bytes"
    )
    body_lines = [
        f"- {f.target_name} (appended {len(f.detail_lines)}-line detail block from index)"
        for f in plan.flattens
    ]
    body_lines += [
        f"- {t.target_name} (moved overlong hook from index, {len(t.full_hook)} chars)"
        for t in plan.tightens
    ]
    line_delta = plan.original_lines - plan.projected_lines
    byte_delta = plan.original_bytes - plan.projected_bytes
    body_lines.append(
        f"- MEMORY.md (-{line_delta} lines, -{byte_delta} bytes)"
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
    print(
        f"\nerror: commit failed ({result.error_kind}): {result.error_message}\n"
        "  file changes are preserved; review `git status` and commit them manually",
        file=sys.stderr,
    )
    return 1


def run(
    cfg: PathConfig,
    *,
    apply: bool = False,
    commit: bool = False,
    commit_author: str | None = None,
) -> int:
    require_unified_layout(cfg, "compact")
    if not apply:
        return _run(
            cfg,
            apply=False,
            commit=commit,
            commit_author=commit_author,
        )

    try:
        recovery_pending = has_pending_transaction_recovery(cfg.memory_dir)
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor compact: transaction recovery incomplete: {exc}",
            file=sys.stderr,
        )
        return 2

    index_path = cfg.memory_dir / "MEMORY.md"
    if not recovery_pending and not index_path.exists():
        try:
            recovery_pending = has_pending_transaction_recovery(cfg.memory_dir)
        except TransactionRecoveryError as exc:
            print(
                f"memory-doctor compact: transaction recovery incomplete: {exc}",
                file=sys.stderr,
            )
            return 2
        if not recovery_pending:
            return _run(
                cfg,
                apply=False,
                commit=False,
                commit_author=commit_author,
            )
    if not recovery_pending:
        try:
            preflight_managed_artifact(
                index_path, label="memory index", required=True
            )
        except TransactionRecoveryError as exc:
            print(
                f"memory-doctor compact: refusing unsafe transaction artifact: {exc}",
                file=sys.stderr,
            )
            return 2
    if not recovery_pending:
        try:
            plan = plan_compaction(
                cfg.memory_dir,
                cfg.max_lines,
                max_hook_chars=cfg.max_hook_chars,
            )
            index_text = index_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            try:
                recovery_pending = has_pending_transaction_recovery(cfg.memory_dir)
            except TransactionRecoveryError as exc:
                print(
                    f"memory-doctor compact: transaction recovery incomplete: {exc}",
                    file=sys.stderr,
                )
                return 2
            if not recovery_pending:
                return _run(
                    cfg,
                    apply=False,
                    commit=False,
                    commit_author=commit_author,
                )
    if not recovery_pending:
        has_action = (
            plan.original_lines > cfg.max_lines
            or plan.original_bytes > cfg.max_bytes
            or bool(plan.flattens)
            or bool(plan.tightens)
            or _index_has_normalizable(index_text)
        )
        if not has_action or plan.missing_targets:
            try:
                recovery_pending = has_pending_transaction_recovery(cfg.memory_dir)
            except TransactionRecoveryError as exc:
                print(
                    f"memory-doctor compact: transaction recovery incomplete: {exc}",
                    file=sys.stderr,
                )
                return 2
            if not recovery_pending:
                return _run(
                    cfg,
                    apply=False,
                    commit=False,
                    commit_author=commit_author,
                )
    if not recovery_pending:
        rc = _apply_preflight(
            cfg,
            plan,
            index_path,
            commit=commit,
            commit_author=commit_author,
        )
        if rc != 0:
            return rc

    try:
        preflight_transaction_capabilities(cfg.memory_dir)
        transaction = ApplyTransaction(cfg.memory_dir)
    except (OSError, RuntimeError, TransactionRecoveryError) as exc:
        print(
            f"memory-doctor compact: transaction recovery incomplete: {exc}",
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
                    "memory-doctor compact: recovered an interrupted apply transaction",
                    file=sys.stderr,
                )
            return _run(
                cfg,
                apply=True,
                commit=commit,
                commit_author=commit_author,
                transaction=transaction,
            )
    except TransactionRecoveryError as exc:
        print(
            f"memory-doctor compact: transaction recovery incomplete: {exc}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        if not entered:
            print(
                f"memory-doctor compact: transaction recovery incomplete: {exc}",
                file=sys.stderr,
            )
            return 2
        outcome = "changes preserved" if transaction.committed else "changes rolled back"
        print(
            f"memory-doctor compact: apply failed ({outcome}): {exc}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        if not entered:
            print(
                f"memory-doctor compact: transaction recovery incomplete: {exc}",
                file=sys.stderr,
            )
            return 2
        outcome = "changes preserved" if transaction.committed else "changes rolled back"
        print(
            f"memory-doctor compact: apply failed ({outcome}): {exc}",
            file=sys.stderr,
        )
        return 1
