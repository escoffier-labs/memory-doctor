# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - Unreleased

### Added

- Split card-directory support for read-only `status` and `lint` through `--cards-dir` or `MEMORY_DOCTOR_CARDS_DIR`. `ingest` and `compact` reject split layouts, including dry runs.
- Bounded handoff ingestion: parsing now rejects handoff files over 1 MiB and suggested card content over 256 KiB before changing a target card, with the configured byte limit included in parse errors.
- Apply operations now use a per-memory-directory exclusive lock and recovery journal, restoring card, index, and handoff state after partial write or move failures.
- Byte-size awareness for MEMORY.md. The Claude Code harness silently drops index content beyond a ~24.4KB read limit, so `status` now reports a byte threshold (default 24000) alongside the line threshold, with OVER/ok markers and new `over_bytes` + `max_bytes` JSON fields. Configure via `--max-bytes N` or `MEMORY_DOCTOR_MAX_BYTES`.
- `compact` now tightens overlong single-line index entries as well as multi-line ones. When a one-line hook exceeds `max_hook_chars` (default 140) and its linked card exists, the full hook is appended to the card under an idempotent `## From index (date)` breadcrumb and the index line is rewritten with a word-boundary-truncated hook. No pointer or content is lost. Re-running is a no-op.
- `compact` now triggers when MEMORY.md is over EITHER the line threshold OR the byte threshold, so an index of long single-line entries no longer slips past compaction.

### Changed

- README documents card-directory configuration, actual path defaults, index-link scanning, and a temporary split-layout example. Repository guidance describes transaction ownership and containment for each artifact.
- Environment-enabled commit mode now prints an explicit notice, while
  `--no-commit` suppresses both the mode and notice. Commit author overrides
  from CLI flags and environment variables now reject control characters and
  malformed names or email addresses before writes.
- Ingest refuses collisions in `processed/<handoff-name>` before mutation, and
  the unused Git-only rollback helper has been removed in favor of transaction
  snapshots that work with or without Git.
- Git preflight now resolves operation state in linked worktrees, fails closed when `git status` errors, and parses NUL-delimited porcelain output for renamed and space-containing paths.
- Verification now builds and installs the wheel in an isolated environment, smokes the installed console script, and checks package metadata against the single-sourced module version.
- `init-git` now reports subprocess failures without tracebacks, validates Git identity before staging, limits the initial commit to intended memory files, and resumes repositories that have no first commit.
- Atomic writes now retain existing POSIX file modes, sync replacement content before the rename, and sync the parent directory where supported.
- README now leads with a recorded terminal demo (`docs/assets/memory-doctor-check.svg`, reproducible from `memory-doctor-check.cast`) of `status` + `lint`, and adds `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and issue / pull-request templates.
- README adopts the fleet adoption-upgrade layout: a what / why / how-it-differs opener, a prominent website link, a keyword-rich "What it does" section, a copy-paste quickstart, and "Why not other tools?" plus "What memory-doctor is not" sections.

- `compact` normalizes unicode punctuation (em dash, en dash, horizontal bar, and the arrow / >= / <= / approx / middot glyphs) to ASCII on every line it rewrites plus a final whole-file pass on apply. Link targets are left untouched.
- `compact` no longer gives up with "No multi-line entries to flatten" when there are overlong single-line hooks or unicode to scrub. The "no action needed" message now prints only when MEMORY.md is genuinely clean and under both thresholds.

### Fixed

- `status` and `ingest` exclude the handoff format template (`TEMPLATE.md`, case-insensitive) from pending handoffs.
- `compact` removes only the two-character continuation prefix when moving index details into cards, preserving nested list and code indentation and trailing whitespace (#33).
- `lint` checks the file portion of index Markdown targets with fragments and retains the original target in diagnostics (#34).
- Removed duplicate card-directory initialization in `PathConfig` (#36).
- Root `error.log` and generated `.graphtrail/` state are ignored by Git (#38).

## [0.2.0] - 2026-06-10

### Added

- Continuous integration workflow running pytest on a Python 3.10 to 3.13 matrix.
- Publish-on-tag workflow that builds the sdist and wheel and uploads to PyPI.
- Resilient fallback for the `MEMORY_INDEX_MAX_LINES` budget when `brigade.budgets` is unavailable. brigade remains the canonical source of truth.

### Changed

- Consume the MEMORY.md index line budget from `brigade.budgets` instead of a hardcoded constant.
