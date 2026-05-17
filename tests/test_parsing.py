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


def test_parse_handoff_content_keeps_internal_headings(tmp_path):
    h = tmp_path / "h.md"
    h.write_text(
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\nfoo.md\n\n"
        "## Suggested card content\n---\nname: foo\n---\n\n## Section A\nbody A\n\n## Section B\nbody B\n"
    )
    parsed = parse_handoff(h)
    assert "## Section A" in parsed.content
    assert "## Section B" in parsed.content
    assert "body B" in parsed.content


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
