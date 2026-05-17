# memory-doctor Design

A maintenance CLI for the Claude Code / OpenClaw file-based memory system. Four verbs - `status`, `ingest`, `compact`, `lint` - that keep a long-running memory directory healthy without manual file edits. Default dry-run on anything that writes; `--apply` to commit.

## Problem

The Claude Code memory system at `~/.claude/projects/<scope>/memory/` is a directory of markdown cards plus a `MEMORY.md` index. Healthy maintenance has three recurring pain points the operator hits weekly:

1. **MEMORY.md hits the truncation threshold.** The harness loads the index up to ~200 lines, then truncates the rest. Today the index is 201 lines and the warning fires at every session start. Compacting entries by hand is tedious and the operator forgets which sections are noisy.

2. **Handoffs accumulate unprocessed.** Sessions write memory handoffs to `~/.openclaw/workspace/.claude/memory-handoffs/` with a "Recommended memory action" + "Target card" + "Suggested card content" block. There is a standalone ingest script but it requires the operator to invoke it by hand; two handoffs sat unprocessed from yesterday's sessions. The May 6 ingest TODO has been pending for over a week.

3. **Dead `[[wiki-link]]` references rot silently.** Cards reference each other with `[[card-name]]` linkers. When a card gets renamed or removed, the references become dead links. Nothing catches this until an agent tries to resolve the link and finds nothing.

There is no single command today that says "tell me the state of memory" or "ingest what's pending" or "scan for dead links." The operator runs ad-hoc shell pipelines or asks an agent to do it.

## Goal

Ship a `pipx install memory-doctor` CLI that gives the operator four verbs:

```
memory-doctor status              # read-only summary
memory-doctor ingest [--apply]    # promote pending handoffs into cards
memory-doctor compact [--apply]   # shrink MEMORY.md back under the threshold
memory-doctor lint                # find dead [[wiki-links]]
```

`status` and `lint` are read-only. `ingest` and `compact` default to dry-run; the operator passes `--apply` to actually write. The operator runs `status` once per session start (or wires it into a hook later) to see what needs attention; they run `ingest --apply` when handoffs are pending; they run `compact --apply` when MEMORY.md crosses 180 lines; they run `lint` periodically to catch link rot.

## Non-goals

- **No LLM calls.** Compact is rule-based: flatten multi-line entries into one-liners, move detail to topic files. No model-generated summarization in v1.
- **No semantic dedup.** If two entries say the same thing in different words, the operator decides. The tool reports candidates, not auto-merges.
- **No remote sync.** Operates on local files only. Memory directories live wherever they live; no git push, no upload, no remote.
- **No reformatting of card bodies.** The tool reads frontmatter and wiki-links; it doesn't rewrite a card's content style.
- **No watch mode / daemon.** Each invocation is a single shot. The operator can wire it into cron or a session-start hook themselves.
- **No multi-scope discovery.** One memory dir, one handoffs dir, per invocation. Multiple projects mean running the CLI multiple times with different paths.
- **No publish to PyPI in v1.** Install via `pipx install git+https://github.com/solomonneas/memory-doctor` or local `pipx install .`.

## Architecture

Standard Python package, src-layout, single console_script entry point.

```
~/repos/memory-doctor/
├── pyproject.toml
├── src/memory_doctor/
│   ├── __init__.py
│   ├── cli.py           # argparse, dispatch to verb modules
│   ├── paths.py         # resolve memory + handoffs dirs from flags/env/defaults
│   ├── parsing.py       # frontmatter + wiki-link extraction; handoff section parsing
│   ├── status.py        # status verb
│   ├── lint.py          # dead-link scanner
│   ├── ingest.py        # handoff -> card promotion
│   └── compact.py       # MEMORY.md compaction
├── tests/
│   ├── conftest.py      # fixture builders (write a fake memory dir + handoffs dir)
│   ├── test_paths.py
│   ├── test_parsing.py
│   ├── test_status.py
│   ├── test_lint.py
│   ├── test_ingest.py
│   ├── test_compact.py
│   └── test_cli.py
├── README.md
├── LICENSE
└── .gitignore
```

Each verb module exports a single `run(args, ...) -> int` function returning a Unix exit code. `cli.py` does argparse and dispatch only. `parsing.py` holds the regex / frontmatter / handoff-section helpers shared across verbs. No external runtime deps beyond stdlib; no `pyyaml` needed - the frontmatter format used here is simple key-value (`name: foo\ndescription: bar\n`).

## Path resolution

Order: explicit flag > env var > default.

| What | Flag | Env | Default |
|---|---|---|---|
| Memory dir (cards + MEMORY.md) | `--memory-dir PATH` | `MEMORY_DOCTOR_MEMORY_DIR` | `~/.claude/projects/-home-clawdbot/memory/` |
| Handoffs dir | `--handoffs-dir PATH` | `MEMORY_DOCTOR_HANDOFFS_DIR` | `~/.openclaw/workspace/.claude/memory-handoffs/` |
| MEMORY.md threshold (lines) | `--max-lines N` | `MEMORY_DOCTOR_MAX_LINES` | `180` |

Resolution lives in `paths.py` and is shared across all verbs. Bad inputs (missing dir, file-instead-of-dir) raise a typed error with a clear message at CLI startup, not deep inside a verb.

## Verb behaviors

### `status`

Read-only. Reports:

- Memory dir path, total cards (`.md` files in the dir, minus `MEMORY.md`), MEMORY.md line count + bytes
- Handoffs dir path, # pending (top-level `*.md`), # already processed (`processed/*.md`)
- Oldest pending handoff age in days (file mtime)
- # dead wiki-links across all cards (calls into `lint`)
- Whether MEMORY.md is over the configured threshold

Exits 0 always. Output is human-readable by default; `--json` emits a structured payload.

### `lint`

Read-only. Walks all `.md` files in `memory-dir`, extracts every `[[link-text]]` occurrence, and checks for each whether `<memory-dir>/<link-text>.md` exists (case-insensitive, allowing the `cards/` prefix to be stripped if present).

Reports dead links grouped by source file:

```
prompt-caching-implementation.md
  [[cache-control-ttl-explainer]] - no card found

dani-job-hunt-platform-drivers.md
  [[ziprecruiter-glassdoor-platform-drivers]] - no card found
```

For each dead link, suggests the closest-match existing card name (Levenshtein distance ≤ 3). When the closest match equals the dead link with only case/whitespace differences, surfaces that as a high-confidence fix.

Exits 0 when zero dead links; exits 1 when any dead links exist (so CI / pre-commit hooks can gate on it).

### `ingest`

Sweeps `<handoffs-dir>/*.md` (top-level only; ignores the `processed/` subdir).

For each pending handoff file, parses these sections (case-insensitive heading match, tolerant whitespace):

- `## Recommended memory action` -> next non-empty line, one of `create-card` / `update-card` / `no-card`
- `## Target card` -> next non-empty line, the filename (strip optional surrounding backticks/quotes and optional `cards/` prefix)
- `## Suggested card content` -> the entire body until the next `## ` heading (or EOF)

Action handling:

- `create-card`: write `<memory-dir>/<target>.md` with the suggested content. If the file already exists and content is byte-identical, treat as no-op (move to processed). If exists with different content, surface as conflict; default to skip (do not overwrite). `--force` overwrites.
- `update-card`: append the suggested content (separated by a blank line) to `<memory-dir>/<target>.md`. If target does not exist, surface as error (skipped).
- `no-card`: just move the handoff to processed; no card write.

On `--apply`, move the handoff file to `<handoffs-dir>/processed/<basename>`. On dry-run, only report what would happen.

Exits 0 on clean run (no skips/conflicts), 1 if any handoff was skipped due to conflict or missing target, 2 on parse failure.

### `compact`

Reads MEMORY.md. Counts lines. If <= threshold (default 180), reports "no action needed" and exits 0.

If over threshold:

1. Parses the section structure (top-level `## <Section>` headings followed by `- [Title](file.md) — hook` bullets)
2. Identifies multi-line entries - entries that span more than one line because their hook overflowed or someone hand-edited multi-line content into the index
3. For each multi-line entry: extracts the body beyond the one-liner, appends it to the target topic file under a `## From index (YYYY-MM-DD)` section, rewrites the index entry to a single-line hook
4. Reports the line-count delta and lists each flattened entry

Dry-run shows the proposed changes; `--apply` writes both MEMORY.md (in place) and the target topic files (append).

Refuses to compact if a target topic file doesn't exist (would orphan the content). Refuses to compact if the proposed change would still leave MEMORY.md over threshold - the operator gets a "compact alone won't bring you under the limit" report and a list of candidate sections to archive manually.

Hard rule: compact never deletes any content. Every flattened detail moves into a topic file; nothing is dropped.

## Data flow

```
                                       MEMORY_DOCTOR_MEMORY_DIR
                                       (resolved by paths.py)
                                                |
                       +------------------------+--------------------+
                       |                                             |
                       v                                             v
    status: read MEMORY.md + walk cards + count handoffs    lint: walk cards, regex [[link]],
            -> human/json report                                  check target exists, report

                                       MEMORY_DOCTOR_HANDOFFS_DIR
                                                |
                                                v
                       ingest: parse pending handoffs,
                               apply action (create/update/none),
                               move handoff to processed/

                                       MEMORY.md
                                                |
                                                v
                       compact: read, parse sections, flatten multi-line entries,
                                append detail to topic files, rewrite index in place
```

No verb calls another verb at the file level. `status` reuses `lint`'s dead-link counter via a shared function in `lint.py`. `compact` reuses `parsing.py`'s frontmatter helpers.

## Error handling

- Missing memory dir / handoffs dir at startup: clear "not found" error with the resolved path; exit 2.
- Handoff missing a required section: surface in the dry-run report, skip the file, do NOT move it to processed.
- Card write conflict on `ingest` without `--force`: surface, skip, leave handoff in place.
- MEMORY.md write failure mid-compact: defer the file move; never leave an inconsistent on-disk state. Compact writes the topic-file appends FIRST, then the MEMORY.md rewrite LAST, so a crash in the middle leaves topic files with redundant copies (recoverable) rather than an empty MEMORY.md (data loss).
- Bad frontmatter on a card: lint surfaces; doesn't crash.

## Testing

pytest, no external network. Each test seeds a tmp_path with a fake memory dir + handoffs dir via fixtures in `conftest.py`, runs the verb against it, asserts file state + stdout.

Coverage:

- `test_paths.py` (6): flag override > env > default; tilde expansion; missing dir; file-not-dir; threshold default + override.
- `test_parsing.py` (8): frontmatter extract (well-formed, missing, malformed); wiki-link extract (basic, multiple-per-line, escaped, nested); handoff section parse (template-compliant, missing-action, missing-target, multi-paragraph content).
- `test_status.py` (4): counts cards correctly, reports threshold breach, --json shape, handles empty dirs.
- `test_lint.py` (6): finds dead link; ignores live link; case-insensitive resolution; strips `cards/` prefix; Levenshtein suggestion; exit code 1 on dead links.
- `test_ingest.py` (8): create-card happy path; create-card conflict skip; create-card --force overwrite; update-card happy path; update-card missing-target error; no-card moves to processed; dry-run leaves no side effects; multi-handoff batch.
- `test_compact.py` (6): under-threshold no-op; flatten multi-line entry; refuse if target missing; refuse if won't get under threshold; topic-file-append shape; MEMORY.md write-last invariant.
- `test_cli.py` (5): each verb dispatches; bad verb error; --memory-dir override threads through; --json flag works on status; --apply only mutates on real verbs.

Target: ~43 tests. All hermetic.

## Acceptance criteria

1. `pip install -e .` (or `pipx install .` from the repo) puts a `memory-doctor` command on `$PATH`.
2. `memory-doctor status` against the real user paths prints the expected stats (cards count, MEMORY.md line count, pending handoffs, dead links) without exception.
3. `memory-doctor lint` against the real user paths exits 0 if no dead links, 1 if any. Includes suggestions.
4. `memory-doctor ingest` (dry-run) against the real user paths reports the 2 currently-pending handoffs and what actions they would take, without modifying anything.
5. `memory-doctor ingest --apply` processes the same handoffs and moves them to `processed/`.
6. `memory-doctor compact` (dry-run) against MEMORY.md at 201 lines reports proposed flattens; `--apply` brings it under 180.
7. `pytest -q` passes ~43 tests, all hermetic (no real-user-path access from tests).
8. `--memory-dir <path>` and `--handoffs-dir <path>` and `MEMORY_DOCTOR_*` env vars all override defaults correctly.
9. README documents install + usage + the four verbs + the env vars.

## Out of scope, captured

- LLM-assisted compaction summarization (could land as v2).
- MEMORY.md auto-rebuild from cards' frontmatter (interesting but invasive).
- Cross-machine memory sync.
- A `git commit` integration that wraps `--apply` in a commit per verb.
- Wire-up to OpenClaw as a plugin (the CLI is enough; could expose as MCP later if useful).
- `memory-doctor watch` daemon mode.

## Related context

- Memory layout: `~/.claude/projects/-home-clawdbot/memory/` - 167 cards + MEMORY.md, currently 201 lines (truncation kicks in at ~200).
- Handoffs layout: `~/.openclaw/workspace/.claude/memory-handoffs/` - 2 pending, 100 in `processed/`.
- Handoff template: see `TEMPLATE.md` in that dir; sections are stable.
- Distribution pattern: `pipx install` matches `eero-cli` (already user-installed via pipx).
