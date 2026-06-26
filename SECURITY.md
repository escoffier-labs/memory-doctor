# Security Policy

## Supported versions

memory-doctor is pre-1.0; fixes land on the latest version. Please upgrade before reporting.

## Reporting a vulnerability

Report privately, not in a public issue:

- GitHub: **Security → Report a vulnerability** (private advisory) on this repo, or
- contact the maintainer privately via [@solomonneas](https://github.com/solomonneas)

memory-doctor reads and (with `--apply`) rewrites local memory files. The issues
that matter most here are **data loss or corruption** (a compact or ingest that
drops or mangles content) and any path-handling bug that lets it write outside the
configured memory directory. Include the memory layout and the exact command.

## Scope

In scope: the CLI's file reading and writing, the `--apply` mutations
(`ingest`, `compact`), and the dead-link / threshold checks.

Out of scope: the contents of your memory (that is yours to curate) and
`brigade-cli`, the one runtime dependency (report to its own project).

## Notes

memory-doctor makes no network calls and handles no credentials. `ingest` and
`compact` are dry-run by default; `--apply` is required to write.
