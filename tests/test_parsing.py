from pathlib import Path

import pytest

from memory_doctor.parsing import (
    MAX_HANDOFF_BYTES,
    MAX_SUGGESTED_CONTENT_BYTES,
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


def test_parse_handoff_rejects_file_over_byte_limit(tmp_path: Path):
    h = tmp_path / "oversized.md"
    h.write_bytes(b"x" * (MAX_HANDOFF_BYTES + 1))

    with pytest.raises(HandoffParseError, match=rf"{MAX_HANDOFF_BYTES} byte limit"):
        parse_handoff(h)


def test_parse_handoff_rejects_negative_byte_limit_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    h = tmp_path / "must-not-open.md"

    def fail_open(*args, **kwargs):
        pytest.fail("negative byte limit must be rejected before opening the path")

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(HandoffParseError, match="byte limit must be non-negative"):
        parse_handoff(h, max_handoff_bytes=-1)


def test_parse_handoff_rejects_suggested_content_over_byte_limit(tmp_path: Path):
    h = tmp_path / "oversized-content.md"
    h.write_text(
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\nfoo.md\n\n"
        "## Suggested card content\n"
        + ("x" * (MAX_SUGGESTED_CONTENT_BYTES + 1))
    )

    with pytest.raises(
        HandoffParseError,
        match=rf"Suggested card content exceeds {MAX_SUGGESTED_CONTENT_BYTES} byte limit",
    ):
        parse_handoff(h)


def test_parse_handoff_accepts_suggested_content_at_byte_limit(tmp_path: Path):
    h = tmp_path / "content-at-limit.md"
    h.write_text(
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\nfoo.md\n\n"
        "## Suggested card content\n"
        + ("x" * MAX_SUGGESTED_CONTENT_BYTES)
    )

    parsed = parse_handoff(h)

    assert len(parsed.content.encode("utf-8")) == MAX_SUGGESTED_CONTENT_BYTES


def test_parse_handoff_content_limit_counts_utf8_bytes(tmp_path: Path):
    h = tmp_path / "utf8-content.md"
    h.write_text(
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\nfoo.md\n\n"
        "## Suggested card content\néé"
    )

    with pytest.raises(HandoffParseError, match=r"3 byte limit"):
        parse_handoff(h, max_suggested_content_bytes=3)


def test_parse_handoff_preserves_universal_newline_support(tmp_path: Path):
    h = tmp_path / "carriage-return-lines.md"
    h.write_bytes(
        b"## Recommended memory action\rcreate-card\r\r"
        b"## Target card\rfoo.md\r\r"
        b"## Suggested card content\rbody\r"
    )

    parsed = parse_handoff(h)

    assert parsed.action == "create-card"
    assert parsed.target == "foo.md"
    assert parsed.content == "body"
