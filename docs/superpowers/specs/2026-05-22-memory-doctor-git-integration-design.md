# memory-doctor Git Integration Design (Phase 1, v0.2)

First phase of the v2 feature roadmap. Adds opt-in git commits to `ingest --apply` and `compact --apply` so every write is reviewable and revertable, and adds an `init-git` setup verb so adopting the feature is one command.

This is the smallest, lowest-risk piece of the v2 work. Phases 2-4 (LLM-assisted compact, semantic dedup, MEMORY.md auto-rebuild) build on top of it: those phases write more aggressively and need this audit trail in place first.

## Problem

`ingest --apply` and `compact --apply` mutate files in the memory dir with zero audit trail. Three concrete failure modes follow:

1. **No revert path.** If a compact run mangles a topic file or an ingest run writes a card the operator later regrets, there is no easy undo. The atomic-write pattern protects against partial writes but says nothing about logical correctness.

2. **Silent clobber risk.** If the operator hand-edits a card and forgets, a subsequent `ingest --apply` against a handoff targeting the same card can overwrite the manual edit without warning. Dry-run helps when the operator looks at it, but doesn't survive past the run.

3. **No history for cron-driven runs.** Once any verb runs from cron (planned, not yet wired), the operator loses visibility into what changed and when. A git log answers both questions for free.

## Goal

Make every `--apply` run reviewable and revertable by tying it to a git commit in the memory dir.

- New `--commit` flag (and `MEMORY_DOCTOR_COMMIT=1` env) on `ingest` and `compact` opts each verb into "write files + stage + commit" as one operation.
- New `init-git` verb does the one-time setup so adoption is `memory-doctor init-git && memory-doctor ingest --apply --commit`.
- Off by default. Existing users see no behavior change unless they opt in.
- Pre-flight checks refuse any commit that would clobber uncommitted operator edits, run outside a git repo, or land during a mid-rebase/merge.

## Non-goals

- **No push.** Commits land locally. Pushing to a remote is the operator's call. Cross-machine sync stays out of scope (was already a non-goal in v1).
- **No `git status` on unrelated files.** Other in-flight work in the memory dir is fine; we only check the files this verb is about to touch.
- **No `--no-verify`.** If the operator has pre-commit hooks, they run. Bypassing hooks silently would mask the signal they're there to give.
- **No commit signing config.** Whatever the user's `git config commit.gpgsign` says goes. We don't pass `-S` or `--no-gpg-sign`.
- **No persistent config file.** Flags + env only. A `~/.config/memory-doctor/config.toml` is YAGNI for v0.2.
- **No autosquash / amend.** One commit per verb invocation. Operator can squash manually with `git rebase` if they want.
- **No commit attribution to memory-doctor itself.** No `Generated with`, no `Co-Authored-By`, no version trailer. Subject already says `memory-doctor`.

## CLI surface

Two new things on the CLI:

**New flags** (on `ingest` and `compact`):

```
--commit            stage + commit the verb's file changes after --apply
--no-commit         suppress committing even if MEMORY_DOCTOR_COMMIT=1
--commit-author     "Name <email>" override for this commit only
```

`--commit` without `--apply` is a no-op: print `skipping commit (dry-run)` and exit 0. (Friendlier than erroring; operators experimenting with the flag will hit this case.)

**New env vars:**

```
MEMORY_DOCTOR_COMMIT=1               same effect as --commit
MEMORY_DOCTOR_COMMIT_AUTHOR=...      same effect as --commit-author
```

Precedence: flag > env > default. `--no-commit` always wins.

**New verb:**

```
memory-doctor init-git
```

Runs `git init` in the memory dir, writes a default `.gitignore` (empty initially; reserves the file as a customization point), creates the initial commit:

```
memory: initial import (167 cards, MEMORY.md)
```

Idempotent: refuses with a clear error if `.git/` already exists. Output ends with the new commit SHA so the operator can chain it.

## Commit message format

**Subject:** `memory-doctor <verb>: <count> <noun> <past-tense-action>`

Examples:

```
memory-doctor ingest: 3 handoffs promoted
memory-doctor ingest: 1 handoff promoted, 2 skipped
memory-doctor compact: 5 entries flattened
memory-doctor compact: MEMORY.md 217 → 178 lines
```

Compact prefers the line-count delta when MEMORY.md is the headline change. Mixed-outcome ingest runs report both succeeded and skipped counts so `git log --oneline` reads honestly.

**Body:** one bullet per affected file with a per-file reason.

Ingest example:

```
memory-doctor ingest: 3 handoffs promoted

- cards/openclaw-doctor-prefix-regression-2026-05-06.md (create-card from 2026-05-06_openclaw-doctor-prefix-regression.md)
- cards/codex-builder-agent.md (update-card append from 2026-05-19_codex-builder-thinking.md)
- cards/feedback-no-drive-by-prs.md (update-card append from 2026-05-16_drive-by-prs.md)
```

Compact example:

```
memory-doctor compact: 5 entries flattened, MEMORY.md 217 → 178 lines

- cards/tokenjuice-trial-2026-04-19.md (appended 4-line detail block from index)
- cards/clawhub-openclaw-registry.md (appended 2-line detail block)
- cards/openclaw-plugins-enabled-2026-04-22.md (appended 6-line detail block)
- cards/dani-direct-payer-ats-map.md (appended 3-line detail block)
- cards/postiz-linkedin-wired-but-script-missing.md (appended 2-line detail block)
- MEMORY.md (5 entries flattened to one-liners, -39 lines)
```

**Excluded from every commit message:**

- No `Co-Authored-By` lines
- No `Generated with`, `Created with`, or any AI authorship line
- No `via memory-doctor v0.2.0` trailer

**Empty-commit guard.** If `--apply` produced zero file changes, do not create a commit. Print `no changes to commit` and exit 0.

**One commit per run.** Not per file. Easier to `git revert <sha>` the whole run, easier to scan history. Per-file granularity lives in the body.

## Pre-flight checks

Before any write under `--commit`, three checks. Any failure aborts the run with exit code 2; no files are written, no commit is attempted.

### Check 1: memory dir is a git repo

```
$ memory-doctor ingest --apply --commit
error: --commit requires the memory dir to be a git repo
  memory dir: /home/alice/.claude/projects/-home-alice/memory
  fix: run `memory-doctor init-git` once, then retry
```

Implementation: `git -C <memory-dir> rev-parse --git-dir`. If exit != 0, abort. The memory dir must be its own repo's toplevel, not a subdir of another repo. (Walking up would let memory writes accidentally land in an unrelated parent project's history.)

### Check 2: no uncommitted local changes on files we will touch

1. During the dry-run pass, build the set of target files the verb plans to write.
2. Run `git -C <memory-dir> status --porcelain -- <target-files>`.
3. If any entry shows `M `, ` M`, `MM`, or `??`, abort:

```
error: refusing to commit, target files have uncommitted local changes:
  - cards/codex-builder-agent.md (modified, not staged)
  - cards/feedback-no-drive-by-prs.md (untracked)
fix: review with `git diff`, commit/stash/discard, then retry
```

Untracked files matter: an `ingest --apply` writing to a path the operator created locally but did not commit would clobber their draft. Files the verb will not touch are explicitly ignored.

### Check 3: working tree is in a sane state

Refuse to commit during a merge, rebase, cherry-pick, or bisect. Detect via:

- `.git/MERGE_HEAD` exists
- `.git/rebase-merge/` or `.git/rebase-apply/` exists
- `.git/CHERRY_PICK_HEAD` exists
- `.git/BISECT_LOG` exists

```
error: refusing to commit, git is in the middle of a merge/rebase/cherry-pick
  fix: complete or abort the in-progress operation, then retry
```

Paranoid, but prevents a cron-driven `--apply --commit` from landing a commit in a weird interim state.

### Not checked

- Remote sync. We do not `git fetch` or compare against `origin`. Memory dir may have no remote.
- Other files' staging state. `git commit -- <our-files>` only commits our pathspec; other staged content stays staged.
- Branch name. Operator may commit memory work to whatever branch they like.

## Atomicity and failure modes

The write pipeline under `--apply --commit`:

1. Pre-flight checks (above). Abort early; nothing touched.
2. Atomic write all target files (existing behavior, unchanged).
3. `git add -- <our-files>`.
4. `git commit -m '<subject>' -m '<body>' -- <our-files>`.
5. Print success summary including the new commit SHA.

### Step 2 failure (file write)

Existing atomic-write behavior. Partial failure leaves the target file in its previous state. Exit non-zero, no commit attempted, no cleanup needed.

### Step 3 failure (git add)

Rare. Triggered by mid-run filesystem changes or permission flips. Roll back:

- For previously-tracked files: restore from `git show HEAD:<path>`.
- For newly-created files (no HEAD version): delete.

Then exit 2 with:

```
error: git add failed mid-run; reverted file changes
  reason: <git error output>
```

Rollback is on by default; no flag to disable. Files-on-disk-but-not-staged-or-committed is the worst end state because the operator cannot tell what memory-doctor wrote vs what they wrote.

### Step 4 failure (git commit)

Two sub-cases. Both leave files staged, do not auto-revert.

**Pre-commit hook rejected:**

```
error: pre-commit hook rejected the commit; your file changes are staged but not committed
  files: cards/foo.md, MEMORY.md
  fix: review with `git diff --cached`, fix the hook violation, run `git commit -- <files>` manually
       or: `git restore --staged --worktree -- <files>` to discard
```

Exit 1. Don't auto-revert: the hook fired for a reason, and silently reverting would mask the signal.

**Other commit failure** (disk full, signing key missing, repo corrupted):

Same handling: files staged, not committed, same recovery instructions. Exit 1.

### Step 5 failure

Cannot meaningfully fail; commit already exists. Best-effort print.

### Cron-friendliness

A cron line like:

```
*/30 * * * * memory-doctor ingest --apply --commit >> ~/.openclaw/logs/memory-doctor.log 2>&1
```

is safe under this design. If a pre-flight check or hook rejects, exit is non-zero and the next iteration retries. On-disk state stays consistent regardless of failure point.

## Architecture changes

Single new module plus small touches to existing modules. Total expected diff: ~250 lines added, ~30 modified.

```
src/memory_doctor/
├── git.py             NEW: pre-flight checks, commit, rollback
├── init_git.py        NEW: init-git verb implementation
├── cli.py             MODIFIED: dispatch init-git, thread --commit/--no-commit/--commit-author
├── ingest.py          MODIFIED: call git.commit_run() after successful --apply
├── compact.py         MODIFIED: call git.commit_run() after successful --apply
└── safety.py          MODIFIED: extend with git-state checks (or leave standalone in git.py)
```

`git.py` exports:

```python
def is_git_repo(memory_dir: Path) -> bool
def working_tree_sane(memory_dir: Path) -> tuple[bool, str]  # (ok, reason)
def files_have_uncommitted_changes(memory_dir: Path, files: list[Path]) -> list[tuple[Path, str]]
def commit_run(memory_dir: Path, files: list[Path], subject: str, body: str, author: str | None) -> CommitResult
```

`CommitResult` is a dataclass with `sha`, `staged_files`, `error_kind` (`None` | `"hook"` | `"add"` | `"commit-other"`), `error_message`.

`init_git.py` exports `run(args, ...) -> int`, same shape as other verb modules.

No new external runtime dependencies. All git interaction is via `subprocess.run(["git", ...])`. Stdlib only, matching the existing pattern.

## Testing

~14 new tests, all hermetic. Existing 67 tests remain unchanged.

`tests/test_git.py` (new, ~10 tests):

- `test_commit_flag_creates_commit`: happy path on ingest + compact
- `test_commit_message_subject_format`: counts + verb in subject
- `test_commit_message_body_lists_files`: per-file bullets present, no AI trailers
- `test_no_commit_when_no_changes`: empty-commit guard
- `test_refuse_when_not_git_repo`: Check 1
- `test_refuse_on_uncommitted_target_changes`: Check 2, modified + untracked variants
- `test_refuse_during_merge_state`: Check 3
- `test_rollback_on_git_add_failure`: simulated step-3 failure rolls back files
- `test_pre_commit_hook_failure_leaves_staged`: simulated hook rejection
- `test_commit_flag_noop_without_apply`: `--commit` alone with no `--apply` exits 0 cleanly

`tests/test_init_git.py` (new, ~3 tests):

- `test_init_git_creates_repo_and_initial_commit`
- `test_init_git_refuses_if_already_repo`
- `test_init_git_initial_commit_message_format`

`tests/test_cli.py` (existing, +1):

- `test_init_git_verb_dispatches`

Total suite: 67 → ~81 tests, all hermetic (`tmp_path` + `git init` in fixtures, no real-user paths).

## Configuration recap

Additions to the existing table:

| What | Flag | Env | Default |
|---|---|---|---|
| Commit verb output | `--commit` / `--no-commit` | `MEMORY_DOCTOR_COMMIT` | off |
| Commit author override | `--commit-author "Name <email>"` | `MEMORY_DOCTOR_COMMIT_AUTHOR` | from `git config` in memory dir |

## Acceptance criteria

1. `memory-doctor init-git` against a non-git memory dir creates a repo + initial commit. Idempotent: errors clearly if already a repo.
2. `memory-doctor ingest --apply --commit` against a git-tracked memory dir with pending handoffs produces exactly one commit with the documented subject + body, and moves the handoffs to `processed/`.
3. `memory-doctor compact --apply --commit` against an over-threshold MEMORY.md produces one commit whose subject includes the line-count delta.
4. Any of (not-a-repo, uncommitted target changes, mid-merge) aborts with exit 2 and writes nothing.
5. `--commit` without `--apply` prints `skipping commit (dry-run)` and exits 0.
6. All new tests pass; existing 67 tests unchanged.
7. README documents `--commit`, `--no-commit`, `--commit-author`, the `init-git` verb, and the two new env vars.

## Out of scope, captured

Reserved for later phases:

- **Phase 2, LLM-assisted compact (`v0.3`).** Shared model layer routing through the OpenClaw gateway. First consumer: `compact --llm` rewrites verbose hooks tighter and proposes merge candidates when mechanical flatten alone won't get under threshold.
- **Phase 3, semantic dedup (`v0.4`).** New `dedup` verb. Reuses Phase 2's model layer. Reports candidate clusters, never auto-merges.
- **Phase 4, auto-rebuild MEMORY.md (`v0.5`).** New `rebuild` verb. Walks card frontmatter, regenerates MEMORY.md grouped by type. Diff-based. Preserves hand-curated section headings via an override file if present.

Also explicitly out of scope for v0.2:

- `--push` flag. Pushing is manual.
- Persistent config file at `~/.config/memory-doctor/config.toml`.
- Per-file commit granularity. Run-level commits only.
- Auto-`git init` on first `--commit` use. Explicit `init-git` verb only.
- Commit signing controls. Honors `git config` defaults.
- Detecting and amending the most recent memory-doctor commit (e.g., to roll a same-day ingest into one commit). Operator can `git rebase -i` if desired.

## Related context

- v1 design lives at `docs/memory-doctor-design.md` and lists this work in its "Out of scope, captured" section as "A `git commit` integration that wraps `--apply` in a commit per verb."
- A typical memory dir (e.g., `~/.claude/projects/-home-<user>/memory/`) is not a git repo by default. `init-git` is the entry point for adoption.
- Existing atomic-write + idempotency-marker patterns in `ingest.py` and `compact.py` continue to handle file-level atomicity; this phase adds run-level atomicity on top.
