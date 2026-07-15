# Repository Guidance

## Definition of Done
```
./scripts/verify
```
It runs the full test suite (`python3 -m pytest -q`) from the repo root.
- Before claiming any task complete, run `./scripts/verify` and report the actual result (144 tests collected; the Brigade parity check skips when that optional import is unavailable).
- If anything fails, paste the failure output verbatim and say the task is not done. Never claim success without a fresh passing run.

## Project Shape
- Python CLI (`memory-doctor`) that maintains a file-based memory directory: knowledge cards plus a MEMORY.md index. Five verbs: `status`, `lint`, `ingest`, `compact`, `init-git`.
- Entry point is `memory_doctor.cli:main` (console script `memory-doctor`). One module per concern under `src/memory_doctor/`: status, lint, ingest, compact, init_git, git, parsing, paths, safety.
- `ingest` and `compact` are dry-run by default. `--apply` writes; `--commit` additionally creates one git commit in the memory dir after three pre-flight checks (see `src/memory_doctor/git.py` and the README commit-integration section).
- Runtime dependency: `brigade-cli>=0.8.0`. `src/memory_doctor/paths.py` imports `MEMORY_INDEX_MAX_LINES` from `brigade.budgets` as the canonical default for the MEMORY.md line threshold. If tempted to hardcode that default locally: do not; import it.
- `dist/`, `memory/`, `.brigade/`, `.venv/`, and `.claude/` are local artifacts and are gitignored. Do not commit them. `docs/` holds the design doc plus the spec and plan for the git integration.

## Verification
- Full suite: `python3 -m pytest -q` from the repo root (144 tests, under two seconds). pyproject sets `pythonpath = ["src"]` for pytest, so no editable install is needed.
- Targeted: `python3 -m pytest -q tests/test_<area>.py` (one test file per module: cli, parsing, compact, git, ingest, init_git, paths, lint, safety, status).
- Manual smoke: `PYTHONPATH=src python3 -m memory_doctor.cli status --memory-dir <tmp> --handoffs-dir <tmp>`. Bare `python3 -m memory_doctor.cli` fails outside pytest (module not on path). `.venv/bin/memory-doctor` also works.
- If a command you expect is missing, report the exact error and stop. Do not invent commands or guess flags; check `pyproject.toml` and `--help` first.

## Live-Data Safety (hard rules)
- Default `--memory-dir` and `--handoffs-dir` resolve to the operator's REAL live memory and handoffs dirs, derived from $HOME in `src/memory_doctor/paths.py`. Running `ingest --apply` or `compact --apply` with default paths mutates real operator memory.
- Never run `ingest` or `compact` with `--apply` against default paths. Never run them live at all unless the user explicitly asks for a live run in the current session.
- For development and testing, always pass temp dirs via `--memory-dir`/`--handoffs-dir` or `MEMORY_DOCTOR_MEMORY_DIR`/`MEMORY_DOCTOR_HANDOFFS_DIR`, and use the fixtures under `tests/`.
- All file mutations must go through `atomic_write_text` and the target-containment check in `src/memory_doctor/safety.py` (resolved targets must stay inside the memory dir). New write paths that bypass these are bugs.
- Preserve the commit contract: pre-flight failures abort before any write; if the commit itself fails after writes, leave files staged and exit non-zero.

## Test Discipline
- If a test fails after your change: fix the code or, if the behavior change is intended and user-approved, update the test to assert the new behavior. Never delete, skip, xfail, or loosen a failing test to get green.
- If you cannot make the suite pass, report the exact failing test and error verbatim instead of working around it.

## Pushing
- `core.hooksPath` is `hooks/`, so `hooks/pre-push` runs `brigade guard git` on every push (embedded public-repo policy from `brigade-cli`, plus optional private denylist at `~/.config/content-guard/internal.json`) and blocks the push on violations.
- Never push with `--no-verify` or otherwise bypass the hook. If the hook blocks, report the exact violation output and let the user decide.

## Gotchas
- `--commit` without `--apply` is a deliberate no-op that exits 0. Tests rely on this; do not "fix" it.
- `compact` refuses to flatten an entry whose target topic file is missing, to avoid orphaning content. Keep that behavior.
- When you change CLI flags, defaults, or commit-message shape, update README in the same change (it documents the verbs, config table, and commit-message format).

## Memory Handoff
At the end of any substantial task, write a handoff note to `.claude/memory-handoffs/` using that directory's `TEMPLATE.md`.
Record durable discoveries, gotchas, and decisions. Do not wait to be reminded.
