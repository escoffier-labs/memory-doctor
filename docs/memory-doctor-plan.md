# memory-doctor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (implementer-only, opus-4-7). Steps use `- [ ]` checkboxes.

**Goal:** Ship `memory-doctor` v0.1.0 - a Python CLI pipx-installable from `solomonneas/memory-doctor` providing four verbs (`status`, `lint`, `ingest`, `compact`) for maintaining the Claude Code / OpenClaw memory system.

**Architecture:** Python 3.10+, stdlib-only, src-layout package, single console_script entry point. Each verb is a module exposing a `run(args, ...)` function; `cli.py` does argparse + dispatch. No external runtime deps. pytest for tests with hermetic tmp_path fixtures.

**Tech Stack:** Python 3.10+, argparse, pathlib, re, dataclasses, pytest (dev-only), hatchling (build backend).

---

## File Structure

**Create:**
- `pyproject.toml` (hatchling build, console_script entry, classifiers)
- `src/memory_doctor/__init__.py` (version export)
- `src/memory_doctor/paths.py` (resolve dirs from flag/env/default)
- `src/memory_doctor/parsing.py` (frontmatter, wiki-links, handoff sections)
- `src/memory_doctor/status.py` (status verb)
- `src/memory_doctor/lint.py` (dead-link scanner)
- `src/memory_doctor/ingest.py` (handoff -> card promotion)
- `src/memory_doctor/compact.py` (MEMORY.md compaction)
- `src/memory_doctor/cli.py` (argparse + dispatch)
- `tests/conftest.py` (fixture builders)
- `tests/test_paths.py`
- `tests/test_parsing.py`
- `tests/test_status.py`
- `tests/test_lint.py`
- `tests/test_ingest.py`
- `tests/test_compact.py`
- `tests/test_cli.py`
- `README.md`, `LICENSE`

---

## Phase 1: Package skeleton

### Task 1: pyproject.toml + package skeleton

**Files:**
- Create: `pyproject.toml`, `src/memory_doctor/__init__.py`

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "memory-doctor"
version = "0.1.0"
description = "Maintenance CLI for the Claude Code / OpenClaw file-based memory system."
readme = "README.md"
license = "MIT"
requires-python = ">=3.10"
authors = [{ name = "Solomon Neas", email = "srneas@gmail.com" }]
classifiers = [
  "Development Status :: 3 - Alpha",
  "Environment :: Console",
  "Intended Audience :: Developers",
  "License :: OSI Approved :: MIT License",
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.10",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
]

[project.urls]
Homepage = "https://github.com/solomonneas/memory-doctor"
Repository = "https://github.com/solomonneas/memory-doctor"

[project.scripts]
memory-doctor = "memory_doctor.cli:main"

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.hatch.build.targets.wheel]
packages = ["src/memory_doctor"]
```

- [ ] **Step 2: Write src/memory_doctor/__init__.py**

```python
"""memory-doctor: maintenance CLI for the Claude Code / OpenClaw memory system."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Create venv + install dev deps**

```bash
cd ~/repos/memory-doctor
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
which memory-doctor
```

Expected: `memory-doctor` symlinked into `.venv/bin/`. It will fail to run yet (no cli module), but the install succeeded if the path resolves.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/memory_doctor/__init__.py
git commit -m "chore: scaffold python package + console_script entry"
```

Note: `.venv/` is gitignored already.

---

## Phase 2: Core libraries

### Task 2: paths.py + tests

**Files:**
- Create: `src/memory_doctor/paths.py`, `tests/conftest.py`, `tests/test_paths.py`

- [ ] **Step 1: Write tests/conftest.py (shared fixtures)**

```python
"""Test fixtures: hermetic tmp_path-based memory + handoffs dirs."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Empty memory dir at tmp_path/memory."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def handoffs_dir(tmp_path: Path) -> Path:
    """Empty handoffs dir with processed/ subdir."""
    d = tmp_path / "handoffs"
    d.mkdir()
    (d / "processed").mkdir()
    return d


def write_card(memory_dir: Path, name: str, body: str, frontmatter: dict | None = None) -> Path:
    """Write a card to memory_dir/<name>.md."""
    path = memory_dir / f"{name}.md"
    parts: list[str] = []
    if frontmatter:
        parts.append("---")
        for k, v in frontmatter.items():
            parts.append(f"{k}: {v}")
        parts.append("---")
        parts.append("")
    parts.append(body)
    path.write_text("\n".join(parts))
    return path


def write_memory_index(memory_dir: Path, lines: Iterable[str]) -> Path:
    """Write MEMORY.md with the given lines."""
    path = memory_dir / "MEMORY.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def write_handoff(
    handoffs_dir: Path,
    name: str,
    *,
    action: str = "create-card",
    target: str = "new-card.md",
    content: str = "frontmatter and body go here",
) -> Path:
    """Write a handoff file matching the template format."""
    path = handoffs_dir / name
    text = (
        "# Memory Handoff\n\n"
        "## Type\nsetup\n\n"
        "## Title\nTest handoff\n\n"
        "## Summary\nSummary text.\n\n"
        "## Durable facts\n- Fact 1\n\n"
        f"## Recommended memory action\n{action}\n\n"
        f"## Target card\n{target}\n\n"
        f"## Suggested card content\n{content}\n"
    )
    path.write_text(text)
    return path
```

- [ ] **Step 2: Write tests/test_paths.py**

```python
import os
from pathlib import Path

import pytest

from memory_doctor.paths import (
    DEFAULT_MAX_LINES,
    PathConfig,
    resolve_paths,
    PathConfigError,
)


def test_resolves_flag_over_env_over_default(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", "/from-env")
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=None, max_lines=None)
    assert cfg.memory_dir == a


def test_resolves_env_when_no_flag(tmp_path, monkeypatch):
    a = tmp_path / "via-env"
    a.mkdir()
    monkeypatch.setenv("MEMORY_DOCTOR_MEMORY_DIR", str(a))
    cfg = resolve_paths(memory_dir=None, handoffs_dir=None, max_lines=None)
    assert cfg.memory_dir == a


def test_default_max_lines_180(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORY_DOCTOR_MAX_LINES", raising=False)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None)
    assert cfg.max_lines == DEFAULT_MAX_LINES == 180


def test_max_lines_override_via_flag(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=50)
    assert cfg.max_lines == 50


def test_max_lines_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DOCTOR_MAX_LINES", "100")
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    cfg = resolve_paths(memory_dir=str(a), handoffs_dir=str(b), max_lines=None)
    assert cfg.max_lines == 100


def test_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = tmp_path / "memory"; a.mkdir()
    b = tmp_path / "handoffs"; b.mkdir()
    cfg = resolve_paths(memory_dir="~/memory", handoffs_dir="~/handoffs", max_lines=None)
    assert cfg.memory_dir == a
    assert cfg.handoffs_dir == b


def test_missing_memory_dir_raises(tmp_path):
    b = tmp_path / "handoffs"; b.mkdir()
    with pytest.raises(PathConfigError) as exc:
        resolve_paths(memory_dir=str(tmp_path / "nope"), handoffs_dir=str(b), max_lines=None)
    assert "memory" in str(exc.value).lower()


def test_memory_dir_is_file_raises(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("file")
    b = tmp_path / "handoffs"; b.mkdir()
    with pytest.raises(PathConfigError):
        resolve_paths(memory_dir=str(f), handoffs_dir=str(b), max_lines=None)
```

- [ ] **Step 3: Run red**

```bash
pytest tests/test_paths.py -v 2>&1 | tail -15
```

Expected: ImportError on `memory_doctor.paths`.

- [ ] **Step 4: Write src/memory_doctor/paths.py**

```python
"""Path resolution for memory + handoffs dirs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MEMORY_DIR = "~/.claude/projects/-home-clawdbot/memory"
DEFAULT_HANDOFFS_DIR = "~/.openclaw/workspace/.claude/memory-handoffs"
DEFAULT_MAX_LINES = 180


class PathConfigError(Exception):
    pass


@dataclass(frozen=True)
class PathConfig:
    memory_dir: Path
    handoffs_dir: Path
    max_lines: int


def _resolve_dir(flag: str | None, env_key: str, default: str, label: str) -> Path:
    raw = flag or os.environ.get(env_key) or default
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        raise PathConfigError(f"{label} dir not found: {p}")
    if not p.is_dir():
        raise PathConfigError(f"{label} path is not a directory: {p}")
    return p


def resolve_paths(
    *,
    memory_dir: str | None,
    handoffs_dir: str | None,
    max_lines: int | None,
) -> PathConfig:
    md = _resolve_dir(memory_dir, "MEMORY_DOCTOR_MEMORY_DIR", DEFAULT_MEMORY_DIR, "memory")
    hd = _resolve_dir(handoffs_dir, "MEMORY_DOCTOR_HANDOFFS_DIR", DEFAULT_HANDOFFS_DIR, "handoffs")
    if max_lines is not None:
        lines = max_lines
    else:
        env = os.environ.get("MEMORY_DOCTOR_MAX_LINES")
        lines = int(env) if env else DEFAULT_MAX_LINES
    return PathConfig(memory_dir=md, handoffs_dir=hd, max_lines=lines)
```

- [ ] **Step 5: Run green + commit**

```bash
pytest tests/test_paths.py -v 2>&1 | tail -10
git add src/memory_doctor/paths.py tests/conftest.py tests/test_paths.py
git commit -m "feat(paths): config resolution from flag/env/default"
```

---

### Task 3: parsing.py + tests

**Files:**
- Create: `src/memory_doctor/parsing.py`, `tests/test_parsing.py`

- [ ] **Step 1: Write tests/test_parsing.py**

```python
from pathlib import Path

import pytest

from memory_doctor.parsing import (
    extract_frontmatter,
    extract_wiki_links,
    parse_handoff,
    HandoffParseError,
)


def test_extract_frontmatter_well_formed():
    body = "---\nname: foo\ndescription: bar baz\n---\n\nbody text"
    fm, rest = extract_frontmatter(body)
    assert fm == {"name": "foo", "description": "bar baz"}
    assert rest.strip() == "body text"


def test_extract_frontmatter_missing():
    body = "no frontmatter here"
    fm, rest = extract_frontmatter(body)
    assert fm == {}
    assert rest == body


def test_extract_frontmatter_malformed_falls_back():
    body = "---\nnot key value\n---\nbody"
    fm, rest = extract_frontmatter(body)
    assert "not key value" not in fm
    assert "body" in rest


def test_extract_wiki_links_basic():
    text = "see [[card-a]] and [[card-b]] for details"
    links = extract_wiki_links(text)
    assert links == ["card-a", "card-b"]


def test_extract_wiki_links_multiple_per_line():
    text = "[[a]] [[b]] [[c]]"
    assert extract_wiki_links(text) == ["a", "b", "c"]


def test_extract_wiki_links_strips_card_prefix():
    text = "[[cards/foo]] [[bar]]"
    assert extract_wiki_links(text) == ["foo", "bar"]


def test_extract_wiki_links_ignores_markdown_links():
    text = "[link](url.md) and [[real-link]]"
    assert extract_wiki_links(text) == ["real-link"]


def test_parse_handoff_template_compliant(tmp_path: Path):
    h = tmp_path / "h.md"
    h.write_text(
        "# Memory Handoff\n\n"
        "## Type\nsetup\n\n"
        "## Title\nT\n\n"
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\ncards/new-thing.md\n\n"
        "## Suggested card content\n---\nname: new-thing\n---\n\nThe body.\n"
    )
    parsed = parse_handoff(h)
    assert parsed.action == "create-card"
    assert parsed.target == "new-thing.md"
    assert "The body." in parsed.content


def test_parse_handoff_missing_action_raises(tmp_path: Path):
    h = tmp_path / "h.md"
    h.write_text("# Memory Handoff\n## Title\nX\n")
    with pytest.raises(HandoffParseError):
        parse_handoff(h)


def test_parse_handoff_multi_paragraph_content(tmp_path: Path):
    h = tmp_path / "h.md"
    h.write_text(
        "## Recommended memory action\nupdate-card\n\n"
        "## Target card\nfoo.md\n\n"
        "## Suggested card content\nPara one.\n\nPara two.\n"
    )
    parsed = parse_handoff(h)
    assert "Para one." in parsed.content
    assert "Para two." in parsed.content
```

- [ ] **Step 2: Run red**

- [ ] **Step 3: Write src/memory_doctor/parsing.py**

```python
"""Frontmatter, wiki-link, and handoff-section parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
KV_LINE_RE = re.compile(r"^([a-zA-Z0-9_.-]+)\s*:\s*(.*)$")
WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\n]+?)\]\]")


class HandoffParseError(Exception):
    pass


@dataclass(frozen=True)
class ParsedHandoff:
    path: Path
    action: str         # 'create-card' | 'update-card' | 'no-card'
    target: str         # filename, e.g. 'foo.md' (cards/ prefix stripped)
    content: str        # body of the "Suggested card content" section


def extract_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    block = m.group(1)
    fm: dict[str, str] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line:
            continue
        km = KV_LINE_RE.match(line)
        if not km:
            continue
        fm[km.group(1)] = km.group(2).strip()
    rest = text[m.end():]
    return fm, rest


def extract_wiki_links(text: str) -> list[str]:
    out: list[str] = []
    for m in WIKI_LINK_RE.finditer(text):
        raw = m.group(1).strip()
        if raw.startswith("cards/"):
            raw = raw[len("cards/"):]
        out.append(raw)
    return out


def _section_lines(text: str, heading: str) -> list[str] | None:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.IGNORECASE | re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    start = m.end()
    next_heading = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + (next_heading.start() if next_heading else len(text) - start)
    return text[start:end].splitlines()


def _first_nonblank_line(lines: list[str]) -> str:
    for line in lines:
        s = line.strip().strip("`").strip("'\"")
        if s:
            return s
    return ""


def parse_handoff(path: Path) -> ParsedHandoff:
    text = path.read_text()

    action_lines = _section_lines(text, "Recommended memory action")
    if action_lines is None:
        raise HandoffParseError(f"{path}: missing 'Recommended memory action' section")
    action = _first_nonblank_line(action_lines)
    if action not in {"create-card", "update-card", "no-card"}:
        raise HandoffParseError(f"{path}: unknown action {action!r}")

    target_lines = _section_lines(text, "Target card") or []
    raw_target = _first_nonblank_line(target_lines)
    if raw_target.startswith("cards/"):
        raw_target = raw_target[len("cards/"):]
    if raw_target and not raw_target.endswith(".md"):
        raw_target = raw_target + ".md"
    target = raw_target

    content_lines = _section_lines(text, "Suggested card content") or []
    content = "\n".join(content_lines).strip()

    if action in {"create-card", "update-card"}:
        if not target:
            raise HandoffParseError(f"{path}: action {action} requires 'Target card'")
        if not content:
            raise HandoffParseError(f"{path}: action {action} requires 'Suggested card content'")

    return ParsedHandoff(path=path, action=action, target=target, content=content)
```

- [ ] **Step 4: Run green + commit**

```bash
pytest tests/test_parsing.py -v 2>&1 | tail -10
git add src/memory_doctor/parsing.py tests/test_parsing.py
git commit -m "feat(parsing): frontmatter + wiki-links + handoff sections"
```

---

## Phase 3: Read-only verbs

### Task 4: status.py + tests

**Files:**
- Create: `src/memory_doctor/status.py`, `tests/test_status.py`

- [ ] **Step 1: Write tests/test_status.py**

```python
import json
from pathlib import Path

from tests.conftest import write_card, write_handoff, write_memory_index
from memory_doctor.paths import PathConfig
from memory_doctor.status import collect_status, format_status_human, format_status_json


def make_cfg(memory_dir: Path, handoffs_dir: Path, max_lines: int = 180) -> PathConfig:
    return PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=max_lines)


def test_counts_cards_excluding_memory_index(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["# Memory Index"])
    write_card(memory_dir, "card-one", "body")
    write_card(memory_dir, "card-two", "body")
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    assert s.cards == 2
    assert s.memory_index_lines == 1


def test_reports_threshold_breach(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, [f"line {i}" for i in range(200)])
    s = collect_status(make_cfg(memory_dir, handoffs_dir, max_lines=180))
    assert s.over_threshold is True
    assert s.memory_index_lines == 200


def test_under_threshold(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, [f"line {i}" for i in range(50)])
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    assert s.over_threshold is False


def test_counts_pending_and_processed_handoffs(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["x"])
    write_handoff(handoffs_dir, "pending-a.md")
    write_handoff(handoffs_dir, "pending-b.md")
    write_handoff(handoffs_dir / "processed", "done.md")
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    assert s.pending_handoffs == 2
    assert s.processed_handoffs == 1


def test_json_shape_contains_all_fields(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["x"])
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    payload = json.loads(format_status_json(s))
    for key in [
        "memory_dir", "handoffs_dir", "cards", "memory_index_lines",
        "memory_index_bytes", "pending_handoffs", "processed_handoffs",
        "dead_links", "oldest_pending_age_days", "over_threshold", "max_lines",
    ]:
        assert key in payload


def test_human_format_does_not_crash_on_empty(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, [])
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    out = format_status_human(s)
    assert "memory dir" in out.lower() or "cards" in out.lower()
```

- [ ] **Step 2: Run red**

- [ ] **Step 3: Write src/memory_doctor/status.py**

```python
"""Status verb: read-only summary of memory health."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from memory_doctor.paths import PathConfig
from memory_doctor.lint import count_dead_links


@dataclass(frozen=True)
class Status:
    memory_dir: str
    handoffs_dir: str
    cards: int
    memory_index_lines: int
    memory_index_bytes: int
    pending_handoffs: int
    processed_handoffs: int
    dead_links: int
    oldest_pending_age_days: float | None
    over_threshold: bool
    max_lines: int


def _count_cards(memory_dir: Path) -> int:
    return sum(1 for p in memory_dir.glob("*.md") if p.name != "MEMORY.md")


def _index_stats(memory_dir: Path) -> tuple[int, int]:
    p = memory_dir / "MEMORY.md"
    if not p.exists():
        return 0, 0
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.count("\n") if text and not text.endswith("\n") else text.count("\n")
    if text and not text.endswith("\n"):
        lines += 1
    return lines, len(raw)


def _handoff_counts(handoffs_dir: Path) -> tuple[int, int, float | None]:
    pending = [p for p in handoffs_dir.glob("*.md")]
    processed = list((handoffs_dir / "processed").glob("*.md")) if (handoffs_dir / "processed").is_dir() else []
    oldest_age: float | None = None
    if pending:
        now = time.time()
        oldest_mtime = min(p.stat().st_mtime for p in pending)
        oldest_age = (now - oldest_mtime) / 86400.0
    return len(pending), len(processed), oldest_age


def collect_status(cfg: PathConfig) -> Status:
    cards = _count_cards(cfg.memory_dir)
    index_lines, index_bytes = _index_stats(cfg.memory_dir)
    pending, processed, oldest = _handoff_counts(cfg.handoffs_dir)
    dead = count_dead_links(cfg.memory_dir)
    return Status(
        memory_dir=str(cfg.memory_dir),
        handoffs_dir=str(cfg.handoffs_dir),
        cards=cards,
        memory_index_lines=index_lines,
        memory_index_bytes=index_bytes,
        pending_handoffs=pending,
        processed_handoffs=processed,
        dead_links=dead,
        oldest_pending_age_days=oldest,
        over_threshold=index_lines > cfg.max_lines,
        max_lines=cfg.max_lines,
    )


def format_status_human(s: Status) -> str:
    lines = [
        f"memory dir:       {s.memory_dir}",
        f"  cards:          {s.cards}",
        f"  MEMORY.md:      {s.memory_index_lines} lines, {s.memory_index_bytes} bytes",
        f"  threshold:      {s.max_lines} ({'OVER' if s.over_threshold else 'ok'})",
        f"  dead links:     {s.dead_links}",
        "",
        f"handoffs dir:     {s.handoffs_dir}",
        f"  pending:        {s.pending_handoffs}",
        f"  processed:      {s.processed_handoffs}",
    ]
    if s.oldest_pending_age_days is not None:
        lines.append(f"  oldest pending: {s.oldest_pending_age_days:.1f} days")
    return "\n".join(lines)


def format_status_json(s: Status) -> str:
    return json.dumps(asdict(s), indent=2)


def run(cfg: PathConfig, *, as_json: bool = False) -> int:
    s = collect_status(cfg)
    print(format_status_json(s) if as_json else format_status_human(s))
    return 0
```

- [ ] **Step 4: Run green + commit**

```bash
pytest tests/test_status.py -v 2>&1 | tail -10
git add src/memory_doctor/status.py tests/test_status.py
git commit -m "feat(status): memory health summary verb"
```

Note: status.py imports `count_dead_links` from lint.py which doesn't exist yet - this is a forward reference. Define a stub in lint.py NOW to keep imports clean:

```bash
mkdir -p src/memory_doctor
cat > src/memory_doctor/lint.py <<'PYEOF'
"""Lint verb: dead [[wiki-link]] scanner (stub - real impl in Task 5)."""
from __future__ import annotations
from pathlib import Path


def count_dead_links(memory_dir: Path) -> int:
    return 0  # implemented in Task 5
PYEOF
git add src/memory_doctor/lint.py
git commit --amend --no-edit
```

---

### Task 5: lint.py + tests

**Files:**
- Modify: `src/memory_doctor/lint.py` (replace stub with real impl)
- Create: `tests/test_lint.py`

- [ ] **Step 1: Write tests/test_lint.py**

```python
from pathlib import Path

from tests.conftest import write_card
from memory_doctor.lint import scan_dead_links, suggest_closest


def test_finds_dead_link(memory_dir):
    write_card(memory_dir, "alpha", "see [[beta]] for details")
    findings = scan_dead_links(memory_dir)
    assert len(findings) == 1
    assert findings[0].source.name == "alpha.md"
    assert findings[0].link == "beta"


def test_ignores_live_link(memory_dir):
    write_card(memory_dir, "alpha", "see [[beta]] for details")
    write_card(memory_dir, "beta", "the target")
    findings = scan_dead_links(memory_dir)
    assert findings == []


def test_case_insensitive_resolution(memory_dir):
    write_card(memory_dir, "alpha", "see [[Beta-Card]] for details")
    write_card(memory_dir, "beta-card", "the target")
    findings = scan_dead_links(memory_dir)
    assert findings == []


def test_strips_cards_prefix(memory_dir):
    write_card(memory_dir, "alpha", "see [[cards/beta]] for details")
    write_card(memory_dir, "beta", "the target")
    findings = scan_dead_links(memory_dir)
    assert findings == []


def test_suggest_closest_picks_levenshtein_match():
    pool = ["adguard-tco-allowlist-2026-04-16", "indeed-rate-limited", "linkedin-easy-apply"]
    sug = suggest_closest("indeed-rate-limit", pool)
    assert sug == "indeed-rate-limited"


def test_exit_code_one_on_dead_links(memory_dir, handoffs_dir, capsys):
    from memory_doctor.paths import PathConfig
    from memory_doctor.lint import run

    write_card(memory_dir, "alpha", "see [[nope]] for details")
    cfg = PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    code = run(cfg)
    assert code == 1


def test_exit_code_zero_when_clean(memory_dir, handoffs_dir):
    from memory_doctor.paths import PathConfig
    from memory_doctor.lint import run

    write_card(memory_dir, "alpha", "no links here")
    cfg = PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    code = run(cfg)
    assert code == 0
```

- [ ] **Step 2: Run red**

- [ ] **Step 3: Replace src/memory_doctor/lint.py**

```python
"""Lint verb: dead [[wiki-link]] scanner."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memory_doctor.parsing import extract_wiki_links
from memory_doctor.paths import PathConfig


@dataclass(frozen=True)
class DeadLink:
    source: Path
    link: str
    suggestion: str | None


def _existing_card_slugs(memory_dir: Path) -> set[str]:
    return {p.stem.lower() for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def suggest_closest(needle: str, pool: list[str], max_distance: int = 3) -> str | None:
    n = needle.lower()
    best: tuple[int, str] | None = None
    for cand in pool:
        d = _levenshtein(n, cand.lower())
        if d <= max_distance and (best is None or d < best[0]):
            best = (d, cand)
    return best[1] if best else None


def scan_dead_links(memory_dir: Path) -> list[DeadLink]:
    slugs = _existing_card_slugs(memory_dir)
    pool = sorted(slugs)
    out: list[DeadLink] = []
    for p in sorted(memory_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        text = p.read_text(errors="replace")
        for raw_link in extract_wiki_links(text):
            slug = raw_link.lower().removesuffix(".md")
            if slug in slugs:
                continue
            suggestion = suggest_closest(slug, pool)
            out.append(DeadLink(source=p, link=raw_link, suggestion=suggestion))
    return out


def count_dead_links(memory_dir: Path) -> int:
    return len(scan_dead_links(memory_dir))


def run(cfg: PathConfig) -> int:
    findings = scan_dead_links(cfg.memory_dir)
    if not findings:
        print("memory-doctor lint: 0 dead links")
        return 0
    current = None
    for f in findings:
        if f.source != current:
            print(f"\n{f.source.name}")
            current = f.source
        sug = f"  (did you mean {f.suggestion}?)" if f.suggestion else ""
        print(f"  [[{f.link}]] - no card found{sug}")
    print(f"\nmemory-doctor lint: {len(findings)} dead link(s)")
    return 1
```

- [ ] **Step 4: Run green + commit**

```bash
pytest tests/test_lint.py -v 2>&1 | tail -10
git add src/memory_doctor/lint.py tests/test_lint.py
git commit -m "feat(lint): dead wiki-link scanner with closest-match suggestions"
```

---

## Phase 4: Write verbs (dry-run default)

### Task 6: ingest.py + tests

**Files:**
- Create: `src/memory_doctor/ingest.py`, `tests/test_ingest.py`

- [ ] **Step 1: Write tests/test_ingest.py**

```python
from pathlib import Path

from tests.conftest import write_handoff
from memory_doctor.paths import PathConfig
from memory_doctor.ingest import run


def cfg(memory_dir, handoffs_dir):
    return PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)


def test_create_card_happy_path(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "h-1.md", action="create-card", target="cards/new-card.md",
                  content="---\nname: new-card\n---\nbody")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert (memory_dir / "new-card.md").exists()
    assert "body" in (memory_dir / "new-card.md").read_text()
    assert (handoffs_dir / "processed" / "h-1.md").exists()
    assert not (handoffs_dir / "h-1.md").exists()


def test_create_card_conflict_skips_without_force(memory_dir, handoffs_dir):
    (memory_dir / "existing.md").write_text("original")
    write_handoff(handoffs_dir, "h-2.md", action="create-card", target="existing.md",
                  content="new content")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 1
    assert (memory_dir / "existing.md").read_text() == "original"
    assert (handoffs_dir / "h-2.md").exists()


def test_create_card_force_overwrites(memory_dir, handoffs_dir):
    (memory_dir / "existing.md").write_text("original")
    write_handoff(handoffs_dir, "h-3.md", action="create-card", target="existing.md",
                  content="new content")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=True)
    assert code == 0
    assert (memory_dir / "existing.md").read_text().strip() == "new content"


def test_update_card_appends(memory_dir, handoffs_dir):
    (memory_dir / "growing.md").write_text("original line\n")
    write_handoff(handoffs_dir, "h-4.md", action="update-card", target="growing.md",
                  content="appended line")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    text = (memory_dir / "growing.md").read_text()
    assert "original line" in text
    assert "appended line" in text


def test_update_card_missing_target_is_error(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "h-5.md", action="update-card", target="absent.md",
                  content="x")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 1
    assert (handoffs_dir / "h-5.md").exists()


def test_no_card_moves_to_processed(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "h-6.md", action="no-card", target="x.md", content="x")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert (handoffs_dir / "processed" / "h-6.md").exists()


def test_dry_run_no_side_effects(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "h-7.md", action="create-card", target="dry.md", content="x")
    run(cfg(memory_dir, handoffs_dir), apply=False, force=False)
    assert not (memory_dir / "dry.md").exists()
    assert (handoffs_dir / "h-7.md").exists()
    assert not (handoffs_dir / "processed" / "h-7.md").exists()


def test_multi_handoff_batch(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "b-1.md", action="create-card", target="a.md", content="a-body")
    write_handoff(handoffs_dir, "b-2.md", action="create-card", target="b.md", content="b-body")
    write_handoff(handoffs_dir, "b-3.md", action="no-card", target="x.md", content="x")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert (memory_dir / "a.md").exists()
    assert (memory_dir / "b.md").exists()
    assert len(list((handoffs_dir / "processed").glob("*.md"))) == 3
```

- [ ] **Step 2: Run red**

- [ ] **Step 3: Write src/memory_doctor/ingest.py**

```python
"""Ingest verb: promote pending handoffs into cards."""
from __future__ import annotations

import shutil
from pathlib import Path

from memory_doctor.parsing import HandoffParseError, ParsedHandoff, parse_handoff
from memory_doctor.paths import PathConfig


def _process_handoff(
    parsed: ParsedHandoff,
    memory_dir: Path,
    handoffs_dir: Path,
    *,
    apply: bool,
    force: bool,
) -> tuple[str, bool]:
    """Returns (message, success). On dry-run, reports without writing."""
    src = parsed.path

    if parsed.action == "no-card":
        msg = f"{src.name}: no-card -> move to processed"
        if apply:
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    target = memory_dir / parsed.target

    if parsed.action == "create-card":
        if target.exists():
            existing = target.read_text()
            if existing.strip() == parsed.content.strip():
                msg = f"{src.name}: create-card -> {target.name} already identical, move to processed"
                if apply:
                    shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
                return msg, True
            if not force:
                return (f"{src.name}: SKIP - {target.name} exists with different content (use --force)", False)
            msg = f"{src.name}: create-card -> {target.name} (FORCE overwrite)"
            if apply:
                target.write_text(parsed.content if parsed.content.endswith("\n") else parsed.content + "\n")
                shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
            return msg, True
        msg = f"{src.name}: create-card -> {target.name}"
        if apply:
            target.write_text(parsed.content if parsed.content.endswith("\n") else parsed.content + "\n")
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    if parsed.action == "update-card":
        if not target.exists():
            return (f"{src.name}: ERROR - update-card target {target.name} does not exist", False)
        msg = f"{src.name}: update-card -> {target.name} (append)"
        if apply:
            existing = target.read_text()
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            target.write_text(existing + sep + parsed.content + "\n")
            shutil.move(str(src), str(handoffs_dir / "processed" / src.name))
        return msg, True

    return (f"{src.name}: unknown action {parsed.action!r}", False)


def run(cfg: PathConfig, *, apply: bool = False, force: bool = False) -> int:
    pending = sorted(p for p in cfg.handoffs_dir.glob("*.md"))
    if not pending:
        print("memory-doctor ingest: no pending handoffs")
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor ingest ({mode}): {len(pending)} handoff(s)")
    all_ok = True
    for p in pending:
        try:
            parsed = parse_handoff(p)
        except HandoffParseError as e:
            print(f"  {p.name}: PARSE ERROR - {e}")
            all_ok = False
            continue
        msg, ok = _process_handoff(parsed, cfg.memory_dir, cfg.handoffs_dir, apply=apply, force=force)
        print(f"  {msg}")
        if not ok:
            all_ok = False

    return 0 if all_ok else 1
```

- [ ] **Step 4: Run green + commit**

```bash
pytest tests/test_ingest.py -v 2>&1 | tail -10
git add src/memory_doctor/ingest.py tests/test_ingest.py
git commit -m "feat(ingest): promote pending handoffs into cards (dry-run default)"
```

---

### Task 7: compact.py + tests

**Files:**
- Create: `src/memory_doctor/compact.py`, `tests/test_compact.py`

- [ ] **Step 1: Write tests/test_compact.py**

```python
from pathlib import Path

from tests.conftest import write_card, write_memory_index
from memory_doctor.paths import PathConfig
from memory_doctor.compact import run, plan_compaction


def cfg(memory_dir, handoffs_dir, max_lines=10):
    return PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=max_lines)


def test_under_threshold_no_op(memory_dir, handoffs_dir, capsys):
    write_memory_index(memory_dir, [f"- entry {i}" for i in range(5)])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=180), apply=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "no action" in out.lower() or "under threshold" in out.lower()


def test_plan_identifies_multiline_entries(memory_dir):
    write_card(memory_dir, "topic-a", "## existing\nbody")
    write_memory_index(memory_dir, [
        "# Memory Index",
        "",
        "## Section",
        "- [topic-a](topic-a.md) - one-liner ok",
        "- [topic-b](topic-b.md) - first line",
        "  with detail on second line that should flatten",
        "- [topic-a](topic-a.md) - another (intentional duplicate)",
    ])
    plan = plan_compaction(memory_dir, max_lines=5)
    assert plan.original_lines > 5
    flatten_targets = [f.target_name for f in plan.flattens]
    assert "topic-b.md" in flatten_targets


def test_apply_flattens_and_writes_topic_file(memory_dir, handoffs_dir):
    write_card(memory_dir, "topic-c", "original body\n")
    write_memory_index(memory_dir, [
        "# Memory Index",
        "",
        "## Stuff",
        "- [topic-c](topic-c.md) - first line of bullet",
        "  continued on second line - this should flatten",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=3), apply=True)
    assert code == 0
    topic = (memory_dir / "topic-c.md").read_text()
    assert "original body" in topic
    assert "From index" in topic
    assert "continued on second line" in topic
    index = (memory_dir / "MEMORY.md").read_text()
    assert "continued on second line" not in index


def test_apply_refuses_when_target_missing(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        "- [absent](absent.md) - line 1",
        "  line 2",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True)
    assert code == 2
    assert (memory_dir / "MEMORY.md").read_text().count("\n") >= 4


def test_dry_run_no_side_effects(memory_dir, handoffs_dir):
    write_card(memory_dir, "topic-d", "body")
    original_index = [
        "# Memory Index",
        "## Stuff",
        "- [topic-d](topic-d.md) - line 1",
        "  line 2 detail",
    ]
    write_memory_index(memory_dir, original_index)
    snapshot = (memory_dir / "MEMORY.md").read_text()
    run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=False)
    assert (memory_dir / "MEMORY.md").read_text() == snapshot


def test_topic_file_append_shape(memory_dir, handoffs_dir):
    write_card(memory_dir, "topic-e", "original")
    write_memory_index(memory_dir, [
        "## Section",
        "- [topic-e](topic-e.md) - a",
        "  b",
    ])
    run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True)
    appended = (memory_dir / "topic-e.md").read_text()
    assert "## From index" in appended
```

- [ ] **Step 2: Run red**

- [ ] **Step 3: Write src/memory_doctor/compact.py**

```python
"""Compact verb: flatten multi-line MEMORY.md entries into topic files."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from memory_doctor.paths import PathConfig


BULLET_RE = re.compile(r"^- \[([^\]]+)\]\(([^)]+)\)\s*(.*)$")
INDENTED_CONTINUATION_RE = re.compile(r"^\s{2,}\S")


@dataclass(frozen=True)
class Flatten:
    line_index: int           # index of the bullet line in MEMORY.md
    title: str
    target_name: str          # filename, e.g. 'topic-b.md'
    bullet_text: str          # the bullet's original hook text (kept in the index)
    detail_lines: list[str]   # the continuation lines (flattened into the topic file)


@dataclass(frozen=True)
class CompactionPlan:
    original_lines: int
    flattens: list[Flatten]
    missing_targets: list[str]
    projected_lines: int


def plan_compaction(memory_dir: Path, max_lines: int) -> CompactionPlan:
    index_path = memory_dir / "MEMORY.md"
    lines = index_path.read_text().splitlines() if index_path.exists() else []
    flattens: list[Flatten] = []
    missing: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = BULLET_RE.match(line)
        if not m:
            i += 1
            continue
        title = m.group(1)
        target_url = m.group(2)
        bullet_text = m.group(3)
        target_name = target_url.removeprefix("cards/")
        details: list[str] = []
        j = i + 1
        while j < len(lines) and INDENTED_CONTINUATION_RE.match(lines[j]):
            details.append(lines[j].strip())
            j += 1
        if details:
            flattens.append(Flatten(
                line_index=i,
                title=title,
                target_name=target_name,
                bullet_text=bullet_text,
                detail_lines=details,
            ))
            if not (memory_dir / target_name).exists():
                missing.append(target_name)
        i = j

    projected = len(lines) - sum(len(f.detail_lines) for f in flattens)
    return CompactionPlan(
        original_lines=len(lines),
        flattens=flattens,
        missing_targets=missing,
        projected_lines=projected,
    )


def _apply_flatten(memory_dir: Path, plan: CompactionPlan) -> None:
    index_path = memory_dir / "MEMORY.md"
    lines = index_path.read_text().splitlines()
    today = dt.date.today().isoformat()

    for flatten in plan.flattens:
        target_path = memory_dir / flatten.target_name
        existing = target_path.read_text()
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        appended = (
            f"{existing}{sep}## From index ({today})\n\n"
            f"{flatten.bullet_text.strip()}\n\n"
            + "\n".join(flatten.detail_lines)
            + "\n"
        )
        target_path.write_text(appended)

    keep: list[str] = []
    skip_indexes: set[int] = set()
    for flatten in plan.flattens:
        for off in range(1, len(flatten.detail_lines) + 1):
            skip_indexes.add(flatten.line_index + off)
    for idx, line in enumerate(lines):
        if idx in skip_indexes:
            continue
        keep.append(line)
    index_path.write_text("\n".join(keep) + ("\n" if keep else ""))


def run(cfg: PathConfig, *, apply: bool = False) -> int:
    index_path = cfg.memory_dir / "MEMORY.md"
    if not index_path.exists():
        print(f"memory-doctor compact: {index_path} does not exist")
        return 0

    plan = plan_compaction(cfg.memory_dir, cfg.max_lines)
    if plan.original_lines <= cfg.max_lines:
        print(f"memory-doctor compact: {plan.original_lines} lines <= {cfg.max_lines}, no action needed")
        return 0

    mode = "APPLY" if apply else "dry-run"
    print(f"memory-doctor compact ({mode}): MEMORY.md {plan.original_lines} -> ~{plan.projected_lines} lines")

    if plan.missing_targets:
        print("\nERROR: target topic files missing for some flatten candidates:")
        for t in plan.missing_targets:
            print(f"  - {t}")
        print("\nRefusing to compact: would orphan content. Create the missing card(s) first.")
        return 2

    if not plan.flattens:
        print("\nNo multi-line entries to flatten. Manual archival of older sections is required.")
        return 0

    print("\nFlatten candidates:")
    for f in plan.flattens:
        print(f"  [{f.title}] -> {f.target_name} (+{len(f.detail_lines)} line(s))")

    if plan.projected_lines > cfg.max_lines:
        print(f"\nWARNING: even after flattening, MEMORY.md would be {plan.projected_lines} lines (still over {cfg.max_lines}).")
        print("Manual archival of older entries is required.")

    if apply:
        _apply_flatten(cfg.memory_dir, plan)
        print(f"\nApplied. MEMORY.md now {plan.projected_lines} lines.")
    return 0
```

- [ ] **Step 4: Run green + commit**

```bash
pytest tests/test_compact.py -v 2>&1 | tail -10
git add src/memory_doctor/compact.py tests/test_compact.py
git commit -m "feat(compact): flatten multi-line index entries into topic files"
```

---

## Phase 5: CLI + integration

### Task 8: cli.py + tests

**Files:**
- Create: `src/memory_doctor/cli.py`, `tests/test_cli.py`

- [ ] **Step 1: Write tests/test_cli.py**

```python
import json
import subprocess
import sys

import pytest

from tests.conftest import write_card, write_handoff, write_memory_index


def run_cli(args, env=None):
    cmd = [sys.executable, "-m", "memory_doctor.cli"] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def test_status_default_dispatches(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["# Memory Index"])
    r = run_cli(["status", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 0
    assert "cards" in r.stdout.lower() or "memory dir" in r.stdout.lower()


def test_status_json_flag(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["# Memory Index"])
    r = run_cli(["status", "--json", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["cards"] == 0


def test_bad_verb_errors():
    r = run_cli(["frobnicate"])
    assert r.returncode == 2
    assert "frobnicate" in (r.stderr + r.stdout).lower() or "invalid choice" in r.stderr.lower()


def test_lint_exit_one_on_dead_links(memory_dir, handoffs_dir):
    write_card(memory_dir, "alpha", "[[ghost]]")
    r = run_cli(["lint", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 1
    assert "ghost" in r.stdout


def test_ingest_dry_run_default(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "h.md", action="create-card", target="x.md", content="x-body")
    r = run_cli(["ingest", "--memory-dir", str(memory_dir), "--handoffs-dir", str(handoffs_dir)])
    assert r.returncode == 0
    assert not (memory_dir / "x.md").exists()
    assert (handoffs_dir / "h.md").exists()
```

- [ ] **Step 2: Write src/memory_doctor/cli.py**

```python
"""Command-line interface for memory-doctor."""
from __future__ import annotations

import argparse
import sys

from memory_doctor import __version__
from memory_doctor.paths import PathConfigError, resolve_paths


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memory-dir", default=None, help="Memory dir (cards + MEMORY.md). Default: ~/.claude/projects/-home-clawdbot/memory")
    p.add_argument("--handoffs-dir", default=None, help="Handoffs dir. Default: ~/.openclaw/workspace/.claude/memory-handoffs")
    p.add_argument("--max-lines", type=int, default=None, help="MEMORY.md threshold (default 180)")


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="memory-doctor", description="Maintenance CLI for the Claude Code / OpenClaw memory system.")
    root.add_argument("--version", action="version", version=f"memory-doctor {__version__}")
    sub = root.add_subparsers(dest="verb", required=True)

    p_status = sub.add_parser("status", help="Print a read-only summary")
    _add_common(p_status)
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of human text")

    p_lint = sub.add_parser("lint", help="Scan for dead [[wiki-links]]; exit 1 if any")
    _add_common(p_lint)

    p_ingest = sub.add_parser("ingest", help="Promote pending handoffs into cards")
    _add_common(p_ingest)
    p_ingest.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")
    p_ingest.add_argument("--force", action="store_true", help="Overwrite existing cards on create-card conflict")

    p_compact = sub.add_parser("compact", help="Flatten multi-line MEMORY.md entries into topic files")
    _add_common(p_compact)
    p_compact.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")

    return root


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = resolve_paths(
            memory_dir=args.memory_dir,
            handoffs_dir=args.handoffs_dir,
            max_lines=args.max_lines,
        )
    except PathConfigError as e:
        print(f"memory-doctor: {e}", file=sys.stderr)
        return 2

    if args.verb == "status":
        from memory_doctor.status import run as run_status
        return run_status(cfg, as_json=args.json)
    if args.verb == "lint":
        from memory_doctor.lint import run as run_lint
        return run_lint(cfg)
    if args.verb == "ingest":
        from memory_doctor.ingest import run as run_ingest
        return run_ingest(cfg, apply=args.apply, force=args.force)
    if args.verb == "compact":
        from memory_doctor.compact import run as run_compact
        return run_compact(cfg, apply=args.apply)
    parser.error(f"unknown verb: {args.verb}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run green + commit**

```bash
pytest tests/test_cli.py -v 2>&1 | tail -10
git add src/memory_doctor/cli.py tests/test_cli.py
git commit -m "feat(cli): argparse dispatch for status|lint|ingest|compact"
```

- [ ] **Step 4: Full suite check**

```bash
pytest 2>&1 | tail -10
```

Expected: ~43 tests, all passing.

---

## Phase 6: Polish + ship

### Task 9: README + LICENSE

**Files:**
- Create: `README.md`, `LICENSE`

- [ ] **Step 1: Write README.md**

```markdown
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
```

- [ ] **Step 2: Write LICENSE (MIT)**

```
MIT License

Copyright (c) 2026 Solomon Neas

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Commit**

```bash
git add README.md LICENSE
git commit -m "docs: README with usage + install + LICENSE"
```

---

### Task 10: pipx install verification + repo + push

**Files:** none

- [ ] **Step 1: Local pipx install from the repo**

```bash
cd ~/repos/memory-doctor
pipx install . 2>&1 | tail -5
which memory-doctor
memory-doctor --version
```

Expected: `memory-doctor` resolves to a pipx-managed shim. `--version` prints `0.1.0`.

- [ ] **Step 2: Smoke against the user's real memory dir (status only - read-only)**

```bash
memory-doctor status
```

Expected: prints stats for the real memory dir. Doesn't write anything.

- [ ] **Step 3: Smoke ingest dry-run (read-only)**

```bash
memory-doctor ingest
```

Expected: reports the 2 currently-pending handoffs with their planned actions. No file is modified, nothing moves to `processed/`.

- [ ] **Step 4: Smoke lint**

```bash
memory-doctor lint || echo "lint found dead links (exit 1)"
```

Expected: reports dead links if any (or "0 dead links" with exit 0).

- [ ] **Step 5: Smoke compact dry-run**

```bash
memory-doctor compact
```

Expected: reports MEMORY.md line count + whether over threshold; lists flatten candidates if any.

- [ ] **Step 6: Create remote + push**

```bash
cd ~/repos/memory-doctor
gh repo create solomonneas/memory-doctor --public --description "Maintenance CLI for the Claude Code / OpenClaw memory system. Four verbs: status, lint, ingest, compact." --source . --remote origin --push=false
git push -u origin master
gh repo view solomonneas/memory-doctor --json url,visibility 2>&1 | head -3
```

Expected: public repo, master branch pushed.

- [ ] **Step 7: No commit needed - push complete**

---

## Self-review

**Spec coverage:**
- Package skeleton + pipx install: Task 1 + Task 10
- paths.py multi-source resolution: Task 2
- parsing.py (frontmatter + wiki-links + handoff sections): Task 3
- status verb: Task 4
- lint verb: Task 5
- ingest verb: Task 6
- compact verb: Task 7
- CLI dispatch: Task 8
- README + LICENSE: Task 9
- Tests for every module: Tasks 2-8
- Real-user-path smokes: Task 10

Every spec acceptance criterion maps to a task.

**Placeholder scan:** No TBDs. All code blocks complete. The forward-reference workaround (Task 4 Step 4 creates a stub `lint.py` that Task 5 replaces) is explicit.

**Type consistency:** `PathConfig` shape is `{memory_dir, handoffs_dir, max_lines}` throughout. Verb modules all export `run(cfg, ...)` returning `int`. `ParsedHandoff` shape is `{path, action, target, content}` consistently.

---

## Execution

After all 10 tasks land:

```bash
cd ~/repos/memory-doctor
pytest
pipx install --force .
memory-doctor status
gh repo view solomonneas/memory-doctor
```
