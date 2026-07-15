# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Byte-size awareness for MEMORY.md. The Claude Code harness silently drops index content beyond a ~24.4KB read limit, so `status` now reports a byte threshold (default 24000) alongside the line threshold, with OVER/ok markers and new `over_bytes` + `max_bytes` JSON fields. Configure via `--max-bytes N` or `MEMORY_DOCTOR_MAX_BYTES`.
- `compact` now tightens overlong single-line index entries, not just multi-line ones. When a one-line hook exceeds `max_hook_chars` (default 140) and its linked card exists, the full hook is appended to the card under an idempotent `## From index (date)` breadcrumb and the index line is rewritten with a word-boundary-truncated hook. No pointer or content is lost; re-running is a no-op.
- `compact` now triggers when MEMORY.md is over EITHER the line threshold OR the byte threshold, so an index of long single-line entries no longer slips past compaction.

### Changed

- Git preflight now resolves operation state in linked worktrees, fails closed when `git status` errors, and parses NUL-delimited porcelain output for renamed and space-containing paths.
- Atomic writes now retain existing POSIX file modes, sync replacement content before the rename, and sync the parent directory where supported.
- README now leads with a recorded terminal demo (`docs/assets/memory-doctor-check.svg`, reproducible from `memory-doctor-check.cast`) of `status` + `lint`, and adds `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and issue / pull-request templates.
- README adopts the fleet adoption-upgrade layout: a what / why / how-it-differs opener, a prominent website link, a keyword-rich "What it does" section, a copy-paste quickstart, and "Why not other tools?" plus "What memory-doctor is not" sections.

- `compact` normalizes unicode punctuation (em dash, en dash, horizontal bar, and the arrow / >= / <= / approx / middot glyphs) to ASCII on every line it rewrites plus a final whole-file pass on apply. Link targets are left untouched.
- `compact` no longer gives up with "No multi-line entries to flatten" when there are overlong single-line hooks or unicode to scrub. The "no action needed" message now prints only when MEMORY.md is genuinely clean and under both thresholds.

## [0.2.0] - 2026-06-10

### Added

- Continuous integration workflow running pytest on a Python 3.10 to 3.13 matrix.
- Publish-on-tag workflow that builds the sdist and wheel and uploads to PyPI.
- Resilient fallback for the `MEMORY_INDEX_MAX_LINES` budget when `brigade.budgets` is unavailable. brigade remains the canonical source of truth.

### Changed

- Consume the MEMORY.md index line budget from `brigade.budgets` instead of a hardcoded constant.
