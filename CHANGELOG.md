# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-10

### Added

- Continuous integration workflow running pytest on a Python 3.10 to 3.13 matrix.
- Publish-on-tag workflow that builds the sdist and wheel and uploads to PyPI.
- Resilient fallback for the `MEMORY_INDEX_MAX_LINES` budget when `brigade.budgets` is unavailable. brigade remains the canonical source of truth.

### Changed

- Consume the MEMORY.md index line budget from `brigade.budgets` instead of a hardcoded constant.
