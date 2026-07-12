"""Lint verb: dead-link scanner for cards ([[wiki-links]]) and MEMORY.md
(markdown bullet targets plus wiki links)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from memory_doctor.parsing import extract_wiki_links
from memory_doctor.paths import PathConfig

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass(frozen=True)
class DeadLink:
    source: Path
    link: str
    suggestion: str | None
    kind: str = "wiki"  # 'wiki' ([[...]]) or 'index' (markdown link in MEMORY.md)


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


def _index_link_targets(text: str) -> list[str]:
    """Local card targets of markdown links in MEMORY.md.

    Skips external URLs, anchors, mailto:, and nested paths (other than the
    conventional 'cards/' prefix, which is stripped): those are not card
    references and would only produce false positives.
    """
    targets: list[str] = []
    for m in MD_LINK_RE.finditer(text):
        target = m.group(2).strip()
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        if target.startswith("cards/"):
            target = target[len("cards/"):]
        if "/" in target or not target.endswith(".md"):
            continue
        targets.append(target)
    return targets


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

    index_path = memory_dir / "MEMORY.md"
    if index_path.exists():
        text = index_path.read_text(errors="replace")
        for raw_link in extract_wiki_links(text):
            slug = raw_link.lower().removesuffix(".md")
            if slug not in slugs:
                out.append(DeadLink(
                    source=index_path, link=raw_link,
                    suggestion=suggest_closest(slug, pool),
                ))
        for target in _index_link_targets(text):
            slug = target.lower().removesuffix(".md")
            if slug not in slugs:
                out.append(DeadLink(
                    source=index_path, link=target,
                    suggestion=suggest_closest(slug, pool), kind="index",
                ))
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
        shown = f"({f.link})" if f.kind == "index" else f"[[{f.link}]]"
        print(f"  {shown} - no card found{sug}")
    print(f"\nmemory-doctor lint: {len(findings)} dead link(s)")
    return 1
