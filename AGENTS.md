# Repository Guidance

## Definition of Done
```
./scripts/verify
```
It runs the full test suite, builds and installs the wheel in a fresh virtual environment, smokes the installed console script, and checks version parity.
- Before claiming any task complete, run `./scripts/verify` and report the actual result (the Brigade parity check skips when that optional import is unavailable).
- If anything fails, paste the failure output verbatim and say the task is not done. Never claim success without a fresh passing run.

## Project Shape
- Python CLI (`memory-doctor`) that maintains a file-based memory directory: knowledge cards plus a MEMORY.md index. Five verbs: `status`, `lint`, `ingest`, `compact`, `init-git`.
- Entry point is `memory_doctor.cli:main` (console script `memory-doctor`). One module per concern under `src/memory_doctor/`: status, lint, ingest, compact, init_git, git, parsing, paths, safety, transaction.
- `ingest` and `compact` are dry-run by default. `--apply` writes. `--commit` creates one git commit in the memory dir after three pre-flight checks (see `src/memory_doctor/git.py` and the README commit-integration section).
- Runtime dependency: `brigade-cli>=0.8.0`. `src/memory_doctor/paths.py` imports `MEMORY_INDEX_MAX_LINES` from `brigade.budgets` as the canonical default for the MEMORY.md line threshold. If tempted to hardcode that default locally: do not. Import it.
- `dist/`, `memory/`, `.brigade/`, `.venv/`, and `.claude/` are local artifacts and are gitignored. Do not commit them. `docs/` holds the design doc plus the spec and plan for the git integration.

## Verification
- Full gate: `./scripts/verify` from the repo root. The pytest phase uses `pythonpath = ["src"]`. The packaging phase separately exercises the built wheel and installed console script.
- Targeted: `python3 -m pytest -q tests/test_<area>.py` (one test file per module: cli, parsing, compact, git, ingest, init_git, paths, lint, safety, status).
- Manual smoke: `PYTHONPATH=src python3 -m memory_doctor.cli status --memory-dir <tmp> --handoffs-dir <tmp>`. Bare `python3 -m memory_doctor.cli` fails outside pytest (module not on path). `.venv/bin/memory-doctor` also works.
- If a command you expect is missing, report the exact error and stop. Do not invent commands or guess flags. Check `pyproject.toml` and `--help` first.

## Live-Data Safety (hard rules)
- Default `--memory-dir` and `--handoffs-dir` resolve to the operator's REAL live memory and handoffs dirs, derived from $HOME in `src/memory_doctor/paths.py`. Running `ingest --apply` or `compact --apply` with default paths mutates real operator memory.
- Never run `ingest` or `compact` with `--apply` against default paths. Never run them live at all unless the user explicitly asks for a live run in the current session.
- For development and testing, always pass temp dirs via `--memory-dir`/`--handoffs-dir` or `MEMORY_DOCTOR_MEMORY_DIR`/`MEMORY_DOCTOR_HANDOFFS_DIR`, and use the fixtures under `tests/`.
- Card targets must pass `resolve_card_target` in `src/memory_doctor/safety.py`, stay inside the resolved memory dir, and reject traversal, symlinks, nested paths, and reserved names.
- `ingest --apply` and `compact --apply` mutations must use an active `ApplyTransaction` from `src/memory_doctor/transaction.py`. It owns the exclusive lock, recovery journal, atomic file replacement through `atomic_write_text`, and handoff moves. Journal originals before mutation and preserve filesystem identity checks and recovery behavior. New apply paths that bypass the transaction are bugs.
- Transaction containment is specific to each artifact: card and index files stay inside the memory dir, while handoff sources and processed destinations stay inside the configured handoffs dir. Private lock and journal state lives beside the memory dir and must retain its validation. Do not apply the card-containment rule to handoff moves or private transaction state.
- Preserve the commit contract: pre-flight failures abort before any write. If the commit itself fails after writes, leave files staged and exit non-zero.

## Test Discipline
- If a test fails after your change: fix the code or, if the behavior change is intended and user-approved, update the test to assert the new behavior. Never delete, skip, xfail, or loosen a failing test to get green.
- If you cannot make the suite pass, report the exact failing test and error verbatim instead of working around it.

## Pushing
- `core.hooksPath` is `hooks/`, so `hooks/pre-push` runs `brigade guard git` on every push (embedded public-repo policy from `brigade-cli`, plus optional private denylist at `~/.config/content-guard/internal.json`) and blocks the push on violations.
- Never push with `--no-verify` or otherwise bypass the hook. If the hook blocks, report the exact violation output and let the user decide.

## Gotchas
- `--commit` without `--apply` is a deliberate no-op that exits 0. Tests rely on this. Do not "fix" it.
- `compact` refuses to flatten an entry whose target topic file is missing, to avoid orphaning content. Keep that behavior.
- When you change CLI flags, defaults, or commit-message shape, update README in the same change (it documents the verbs, config table, and commit-message format).

## Memory Handoff
At the end of any substantial task, write a handoff note to `.claude/memory-handoffs/` using that directory's `TEMPLATE.md`.
Record durable discoveries, gotchas, and decisions. Do not wait to be reminded.
