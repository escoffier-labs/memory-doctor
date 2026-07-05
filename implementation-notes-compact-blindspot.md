# Implementation notes: compact blind spot

Running log of decisions, deviations, and tradeoffs while fixing the
`memory-doctor compact` blind spot discovered during a dogfood session where
MEMORY.md grew to ~50KB and compact could do nothing about it.

## Root cause (the blind spot)

1. `compact` only flattens MULTI-LINE entries (a `- [title](link) hook` bullet
   followed by 2-space-indented continuation lines). When every index entry is
   already a single (long) line, `plan.flattens` is empty and compact prints
   "No multi-line entries to flatten" and gives up.
2. No byte-size awareness. The Claude Code harness silently drops MEMORY.md
   content beyond a ~24.4KB read limit, but memory-doctor only tracked a
   180-LINE threshold.
3. It never normalized em dashes, which violate the user's no-em-dash rule and
   were all over the index.

## Plan

- Change 1: byte-threshold awareness in `status` (status.py, paths.py, cli.py).
- Change 2: a non-lossy "tighten" pass in compact.py for overlong single-line
  hooks: append the full hook into the linked card (idempotent marker +
  `## From index (date)` breadcrumb), rewrite the index line with a truncated
  hook, normalize unicode punctuation to ASCII.
- Change 3: keep all existing flatten + status behavior intact.

## Baseline

- 102 tests passing before changes.
- No ruff/black configured (no `[tool.ruff]`/`[tool.black]` in pyproject, no
  binaries on PATH or in .venv). So only pytest is the gate.

## Decisions

### Change 1: byte-threshold awareness

- `DEFAULT_MAX_BYTES = 24000` lives in paths.py (NOT brigade.budgets) because it
  is a Claude Code harness limit, not a brigade budget. Comment notes the
  ~24.4KB read-ceiling origin.
- `PathConfig` gained `max_bytes: int = DEFAULT_MAX_BYTES` and
  `max_hook_chars: int = 140` (the latter is used by Change 2). Both have
  defaults so existing `PathConfig(...)` constructions in tests keep working.
- `resolve_paths` gained `max_bytes: int | None = None` and a
  `MEMORY_DOCTOR_MAX_BYTES` env path mirroring `MEMORY_DOCTOR_MAX_LINES`.
- `Status` gained `over_bytes` + `max_bytes`. Kept all existing fields, only
  ADDED, so `--json` stays backward compatible. The human format now shows two
  threshold lines: `lines: N / M (ok|OVER)` and `bytes: N / M (ok|OVER)`. The
  old combined `threshold:` line was replaced by the clearer `lines:` line; the
  raw `MEMORY.md: N lines, N bytes` line is unchanged.
- Tests: test_paths.py (default/flag/env/invalid/non-positive for max_bytes),
  test_status.py (`test_over_bytes_flips_at_boundary`,
  `test_human_format_includes_byte_threshold_line`, extended JSON-shape test).

### Change 2: tighten overlong single-line hooks

- New `Tighten` frozen dataclass (line_index, title, target_name, full_hook,
  short_hook) and `_tighten_marker` with a distinct `<!-- compact:tighten:... -->`
  prefix (hashes target+title+full_hook so distinct hooks never collide).
- `_normalize_unicode` maps em/en dash, horizontal bar, and the unicode chars
  arrow/>=/<=/approx/middot to ASCII. `_has_normalizable` detects whether any
  remain. Applied to every rewritten line PLUS a whole-file pass on apply via
  `_rewrite_index_lines` (a single pure projection used both for byte estimation
  in planning and the real rewrite, so projected == actual). Verified the two
  match exactly with a scratch script.
- `_truncate_hook` cuts to `max_hook_chars` at a word boundary and appends
  `...`, counting the ellipsis toward the budget so the visible text never
  exceeds the limit.
- Tighten only fires when the linked card EXISTS. Dangling links are left full
  (the index may be the only record) and merely normalized in place. Unsafe
  targets are collected into `unsafe_targets` and skipped, same as flatten.
- Idempotency: once tightened, the index line is short, so a re-run does not
  re-detect it as a candidate; the card marker also guards the append. Verified
  re-apply is a byte-for-byte no-op on both index and card.
- `plan_compaction` now returns `tightens`, `projected_bytes`, `original_bytes`.
  `run()` triggers when lines > max_lines OR bytes > max_bytes OR there are
  flatten/tighten candidates OR there is unicode to scrub. "No action needed"
  prints only when genuinely nothing. Added a "Tighten candidates" report block.
- DEVIATION from the literal spec wording: the spec said to skip-and-normalize
  dangling links "in place" via the index rewrite; I implemented exactly that.
  No `Tighten` object is created for dangling links (so they are never
  truncated), but the whole-file normalization pass still scrubs their unicode.

### Change 3: existing behavior intact

- All prior flatten + status + cli + git tests pass unchanged. The flatten apply
  path is shared via the unified `_rewrite_index_lines` (skips flatten detail
  lines, swaps tighten short hooks, normalizes everything else). The 180-line
  flatten path is unchanged in behavior.

## Final state

- 119 tests passing (102 baseline + 7 Change 1 + 10 Change 2; some counted in
  the compact file replacing none).
- No ruff/black configured, so pytest is the only gate.
