# Contributing

memory-doctor is a maintenance CLI for the file-based agent memory system. The
bar is "keeps memory healthy without ever losing content."

## Local setup

```bash
python3 -m pip install -e ".[dev]"
scripts/verify          # full pytest suite
```

## What lands easily

- Bug fixes with a test that fails before and passes after
- New read-only checks (extending `status` / `lint`) with tests
- Documentation

## What needs a conversation first

Open an issue before a PR for:

- Anything that **mutates** memory (`ingest` / `compact` behavior) - data safety
  comes first, and these are dry-run by default for a reason
- Changing the exit-code contract (`lint` exits non-zero on dead links) that CI
  and pre-commit hooks depend on

## Rules

- Mutations stay **dry-run by default**; `--apply` is required to write.
- **No real personal memory** in tests or fixtures; use small synthetic samples.
- Conventional commits, no AI co-authorship trailers.
