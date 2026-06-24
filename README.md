# memory-doctor

Maintenance CLI for the Claude Code / OpenClaw file-based memory system. Five verbs:

```
memory-doctor status              # read-only summary
memory-doctor lint                # find dead [[wiki-links]]; exit 1 if any
memory-doctor ingest [--apply]    # promote pending handoffs into cards
memory-doctor compact [--apply]   # flatten/tighten oversized MEMORY.md entries into topic files
memory-doctor init-git            # initialize the memory dir as a git repo (one-time)
```

`ingest` and `compact` default to dry-run; pass `--apply` to actually write.

## Install

```bash
pipx install git+https://github.com/escoffier-labs/memory-doctor
```

Or from a local clone:

```bash
git clone https://github.com/escoffier-labs/memory-doctor && cd memory-doctor
pipx install .
```

Requires Python 3.10+. One runtime dependency: `brigade-cli>=0.8.0` (used for the canonical MEMORY.md line threshold).

## Development

Install test dependencies and run the suite:

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
```

The repo config points pytest at `src/`, so tests also run from a plain checkout without an editable install as long as pytest is available.

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

## License

MIT
