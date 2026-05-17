# memory-doctor

Maintenance CLI for the Claude Code / OpenClaw file-based memory system. Four verbs:

```
memory-doctor status              # read-only summary
memory-doctor lint                # find dead [[wiki-links]]; exit 1 if any
memory-doctor ingest [--apply]    # promote pending handoffs into cards
memory-doctor compact [--apply]   # flatten multi-line MEMORY.md entries into topic files
```

`ingest` and `compact` default to dry-run; pass `--apply` to actually write.

## Install

```bash
pipx install git+https://github.com/solomonneas/memory-doctor
```

Or from a local clone:

```bash
git clone https://github.com/solomonneas/memory-doctor && cd memory-doctor
pipx install .
```

Requires Python 3.10+. No runtime dependencies beyond stdlib.

## Configuration

| What | Flag | Env | Default |
|---|---|---|---|
| Memory dir (cards + MEMORY.md) | `--memory-dir PATH` | `MEMORY_DOCTOR_MEMORY_DIR` | `~/.claude/projects/-home-clawdbot/memory` |
| Handoffs dir | `--handoffs-dir PATH` | `MEMORY_DOCTOR_HANDOFFS_DIR` | `~/.openclaw/workspace/.claude/memory-handoffs` |
| MEMORY.md threshold (lines) | `--max-lines N` | `MEMORY_DOCTOR_MAX_LINES` | `180` |

The defaults are tuned for the OpenClaw layout. Override via flags or env for other setups.

## What each verb does

### `status`

Prints memory dir path, card count, MEMORY.md line+byte count, threshold status, dead-link count, handoffs dir path, pending + processed counts, oldest pending age. Exits 0. `--json` for a structured payload.

### `lint`

Walks every `.md` in the memory dir, extracts `[[wiki-link]]` references, checks whether each target exists. Reports dead links grouped by source file with a closest-match suggestion (Levenshtein distance ≤ 3). Exits 0 if zero dead links, 1 if any (so you can gate a pre-commit hook on it).

### `ingest`

Sweeps the handoffs dir for unprocessed `*.md` files matching the standard handoff template. For each one:

- `Recommended memory action: create-card` writes a new card to the memory dir; skips on conflict (use `--force` to overwrite)
- `Recommended memory action: update-card` appends the suggested content to an existing card; errors if the target is missing
- `Recommended memory action: no-card` just moves the handoff to `processed/`

Successful handoffs are moved into `<handoffs-dir>/processed/`. Dry-run by default; `--apply` writes.

### `compact`

Reads MEMORY.md, counts lines. If above the threshold, identifies multi-line entries (bullets whose detail spans more than one line) and proposes flattening them: keep the one-liner in the index, append the detail to the target topic file under a `## From index (YYYY-MM-DD)` section. Dry-run by default; `--apply` writes (topic files first, MEMORY.md last). Refuses if a target topic file is missing (would orphan content). Warns if compaction alone won't bring MEMORY.md under threshold.

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
