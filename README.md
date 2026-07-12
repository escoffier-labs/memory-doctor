<p align="center">
  <img src="docs/assets/memory-doctor-social-preview.jpg" alt="Memory Doctor banner" width="900">
</p>

<h1 align="center">Memory Doctor</h1>

<p align="center">
  <img src="docs/assets/marks/memory-doctor-circle.svg" alt="" width="40" height="40">
</p>

<p align="center">
  <strong>Agent memory rots quietly. Doctor checks it before sessions pay the cost.</strong>
</p>

<p align="center">
  Maintenance CLI for file-based agent memory: status, lint, ingest, compact for Claude Code and OpenClaw layouts. Much of this is now embedded in brigade-cli as brigade memory.
</p>

<p align="center">
  <a href="https://brigade.tools/memory-doctor">Website</a> &middot; <a href="#install">Install</a> &middot; <a href="https://github.com/escoffier-labs/brigade">brigade memory (embedded)</a>
</p>

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/memory-doctor.svg?branch=master&workflow=ci.yml" alt="CI status">
  <img src="https://shieldcn.dev/badge/license-MIT-green.svg" alt="MIT license">
</p>

## Install

```bash
# Preferred path: embedded in Brigade
pipx install brigade-cli
brigade memory status
brigade memory lint
brigade memory compact

# Standalone (legacy package)
pipx install memory-doctor   # if published; else clone this repo
```

## What it does

| | Job | What you get |
|---|---|---|
| **Status** | See the footprint | Cards, index size, pending handoffs |
| **Lint** | Catch dead links | Broken wiki links and stale structure |
| **Compact** | Stay under budget | Flatten bloated MEMORY.md into topic cards |
| **Ingest** | Promote handoffs | Bridge notes into durable memory carefully |

![Memory Doctor memory care workflow](docs/assets/memory-care-workflow.svg)

Generated from [`docs/assets/workflows/memory-care.json`](docs/assets/workflows/memory-care.json) with `plating workflow`.


## Quickstart

```bash
# Read-only health summary of the memory dir (no writes, exits 0):
memory-doctor status

# Find dead [[wiki-links]] before they rot the index (exits 1 if any):
memory-doctor lint

# Preview promoting pending handoffs into cards (dry-run):
memory-doctor ingest
memory-doctor ingest --apply        # actually write

# Preview compacting an oversized MEMORY.md (dry-run):
memory-doctor compact
memory-doctor compact --apply       # actually write
```

Point it at any memory layout with `--memory-dir` / `--handoffs-dir` or the matching env vars (see [Configuration](#configuration)). The defaults are tuned for the OpenClaw layout.

## Configuration

| What | Flag | Env | Default |
|---|---|---|---|
| Memory dir (cards + MEMORY.md) | `--memory-dir PATH` | `MEMORY_DOCTOR_MEMORY_DIR` | `~/.claude/projects/<project-scope>/memory` |
| Handoffs dir | `--handoffs-dir PATH` | `MEMORY_DOCTOR_HANDOFFS_DIR` | `~/.openclaw/workspace/.claude/memory-handoffs` |
| MEMORY.md threshold (lines) | `--max-lines N` | `MEMORY_DOCTOR_MAX_LINES` | `180` |
| MEMORY.md threshold (bytes) | `--max-bytes N` | `MEMORY_DOCTOR_MAX_BYTES` | `24000` |
| Commit verb output | `--commit` / `--no-commit` | `MEMORY_DOCTOR_COMMIT` | off |
| Commit author override | `--commit-author "Name <e>"` | `MEMORY_DOCTOR_COMMIT_AUTHOR` | from git config |

`<project-scope>` is the dash-prefixed home-dir path Claude Code uses to scope per-project memory (e.g. `-home-alice` for user `alice`, `-home-bob` for user `bob`). The defaults are tuned for the OpenClaw layout. Override via flags or env for other setups.

The byte threshold defaults to 24000 because the Claude Code harness silently drops MEMORY.md content read beyond a ~24.4KB limit. An index that is fine on the line count can still have its tail invisible to the agent, so `status` and `compact` track both.

## What each verb does

### `status`

Prints memory dir path, card count, MEMORY.md line+byte count, line and byte threshold status (each marked ok or OVER), dead-link count, handoffs dir path, pending + processed counts, oldest pending age. Exits 0. `--json` for a structured payload (includes `over_threshold`, `max_lines`, `over_bytes`, `max_bytes`).

### `lint`

Walks every `.md` in the memory dir, extracts `[[wiki-link]]` references, checks whether each target exists. Reports dead links grouped by source file with a closest-match suggestion (Levenshtein distance ≤ 3). Exits 0 if zero dead links, 1 if any (so you can gate a pre-commit hook on it).

### `ingest`

Sweeps the handoffs dir for unprocessed `*.md` files matching the standard handoff template. For each one:

- `Recommended memory action: create-card` writes a new card to the memory dir; skips on conflict (use `--force` to overwrite)
- `Recommended memory action: update-card` appends the suggested content to an existing card; errors if the target is missing
- `Recommended memory action: no-card` just moves the handoff to `processed/`

Successful handoffs are moved into `<handoffs-dir>/processed/`. Dry-run by default; `--apply` writes.

### `compact`

Reads MEMORY.md, counts lines and bytes. Triggers when MEMORY.md is over the line threshold OR the byte threshold. Two non-lossy passes:

- Flatten: for multi-line entries (bullets whose detail spans more than one line), keep the one-liner in the index and append the detail to the target topic file under a `## From index (YYYY-MM-DD)` section.
- Tighten: for single-line entries whose hook exceeds `max_hook_chars` (default 140) and whose linked card exists, append the full hook to the card under the same breadcrumb and rewrite the index line with a word-boundary-truncated hook ending in `...`. Dangling links (no card on disk) are left full, since the index may be the only record. No `](...)` pointer is ever dropped.

Every rewritten line is normalized to ASCII punctuation (em dash, en dash, and a few other glyphs become `-`, `->`, `>=`, `<=`, `~`), with a final whole-file normalization pass on apply. Link targets are never touched. Dry-run by default; `--apply` writes (topic files first, MEMORY.md last). Refuses if a target topic file is missing for a flatten candidate (would orphan content). Warns if compaction alone won't bring MEMORY.md under either threshold. Re-running `--apply` is a no-op.

## Commit integration (v0.2)

`ingest --apply` and `compact --apply` can be tied to a git commit in the memory dir so every write is reviewable and revertable.

```bash
# One-time setup: turn the memory dir into a git repo.
memory-doctor init-git

# Each --apply now produces one commit.
memory-doctor ingest --apply --commit
memory-doctor compact --apply --commit
```

Off by default; opt in via `--commit` or `MEMORY_DOCTOR_COMMIT=1`. `--no-commit` overrides the env var for a single run.

Pre-flight checks (any failure aborts the run, writes nothing):
1. Memory dir is a git repo (otherwise: `run memory-doctor init-git`).
2. No uncommitted local changes on the files this verb would touch (protects in-flight manual edits).
3. Git is not in the middle of a merge, rebase, cherry-pick, or bisect.

Check 2 also runs on plain `--apply` (without `--commit`) whenever the memory dir is a git repo: applying over uncommitted edits would bury them with no committed baseline to diff against. Commit or stash first, then retry.

Commit message shape:

```
memory-doctor ingest: 3 handoffs promoted

- cards/foo.md (create-card from 2026-05-22_foo.md)
- cards/bar.md (update-card append from 2026-05-22_bar.md)
- cards/baz.md (create-card from 2026-05-22_baz.md)
```

No `Co-Authored-By` or `Generated with` trailers; subject already identifies the tool.

`--commit` without `--apply` is a no-op and exits 0 (friendly for experimentation).

If git rejects the commit after writes succeed, for example because a hook fails, memory-doctor leaves the touched files staged and exits non-zero. Review the staged diff, fix the hook failure, then commit or unstage manually.

## Examples

```bash
# Daily morning check:
memory-doctor status

# Drain the inbox:
memory-doctor ingest --apply

# Bring MEMORY.md back under threshold:
memory-doctor compact --apply

# Pre-push hook:
memory-doctor lint
```

## Development

Install test dependencies and run the suite:

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

The repo config points pytest at `src/`, so tests also run from a plain checkout without an editable install as long as pytest is available.

## Why not other tools?

- **mem0, Letta, and hosted memory layers** are built for apps you are shipping, usually behind an API or a server. Memory Doctor is for the agent CLIs you already run, and it never owns your memory. Your cards stay plain markdown on disk, readable and editable without it, and Memory Doctor only checks and tidies what is already there.
- **A harness's own auto-memory** keeps writing to a silo without review and has no concept of dead links or a read-limit cliff. Memory Doctor adds the linter, the byte-aware threshold, and the dry-run-by-default mutations the built-in memory never gave you.
- **A hand-rolled shell script or pre-commit hook** is exactly the thing this replaces. Memory Doctor gives you the dead-link linter, the non-lossy compactor, and the handoff ingester as one tested CLI with a stable exit-code contract, instead of glue you maintain forever.
- **A daemon or background watcher** would be simpler to demo and worse to trust. Memory Doctor only runs when you run a verb, writes nothing without `--apply`, and makes no network calls.

## What memory-doctor is not

Memory Doctor is not a memory store, a hosted service, or a background agent.

It does not:

- run in the background, watch files, or install schedulers
- make network calls or handle credentials
- write anything without an explicit `--apply` (`status` and `lint` never write at all)
- invent or summarize memory content; it checks links, tracks size, moves handoffs, and tightens overlong index lines without losing any pointer

What it edits is mechanical and reversible. The content of your memory is yours to curate.

## License

MIT. See [LICENSE](LICENSE).

---

Project identity: GitHub [`escoffier-labs/memory-doctor`](https://github.com/escoffier-labs/memory-doctor), website [memory-doctor.escoffierlabs.dev](https://memory-doctor.escoffierlabs.dev), PyPI [`memory-doctor`](https://pypi.org/project/memory-doctor/), command `memory-doctor`.
