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


# --- MEMORY.md index scanning ---

def test_index_dead_markdown_link_detected(memory_dir):
    write_card(memory_dir, "alpha", "body")
    (memory_dir / "MEMORY.md").write_text(
        "# Memory Index\n- [Alpha](alpha.md) - fine\n- [Ghost](ghost.md) - dead\n"
    )
    findings = scan_dead_links(memory_dir)
    assert len(findings) == 1
    f = findings[0]
    assert f.source.name == "MEMORY.md"
    assert f.link == "ghost.md"
    assert f.kind == "index"


def test_index_cards_prefix_target_resolves(memory_dir):
    write_card(memory_dir, "alpha", "body")
    (memory_dir / "MEMORY.md").write_text("- [Alpha](cards/alpha.md) ok\n")
    assert scan_dead_links(memory_dir) == []


def test_index_external_links_ignored(memory_dir):
    (memory_dir / "MEMORY.md").write_text(
        "- [site](https://example.com/x.md) t\n"
        "- [anchor](#section) t\n"
        "- [mail](mailto:memory-card.md) t\n"
        "- [nested](docs/notes.md) t\n"
    )
    assert scan_dead_links(memory_dir) == []


def test_index_wiki_link_detected(memory_dir):
    (memory_dir / "MEMORY.md").write_text("see [[ghost]] for details\n")
    findings = scan_dead_links(memory_dir)
    assert len(findings) == 1
    assert findings[0].kind == "wiki"
    assert findings[0].source.name == "MEMORY.md"


def test_index_dead_wiki_link_gets_suggestion(memory_dir):
    write_card(memory_dir, "beta", "content")
    (memory_dir / "MEMORY.md").write_text("see [[betaa]]\n")
    findings = scan_dead_links(memory_dir)
    assert len(findings) == 1
    assert findings[0].kind == "wiki"
    assert findings[0].suggestion == "beta"


# Split cards/index layout (OpenClaw) -------------------------------------


def _split_store(tmp_path):
    """OpenClaw shape: cards in a subdir, MEMORY.md one level up."""
    memory = tmp_path / "memory"
    cards = memory / "cards"
    cards.mkdir(parents=True)
    (cards / "real-card.md").write_text("# Real\n", encoding="utf-8")
    (cards / "linker.md").write_text(
        "Related: [[real-card]] and [[ghost-card]].\n", encoding="utf-8"
    )
    # A daily log beside cards/ must not be mistaken for a card.
    (memory / "2026-08-13.md").write_text("daily log\n", encoding="utf-8")
    (memory / "MEMORY.md").write_text("- [Real](real-card.md)\n", encoding="utf-8")
    return memory, cards


def test_scan_dead_links_honours_split_cards_and_index(tmp_path):
    from memory_doctor.lint import scan_dead_links

    memory, cards = _split_store(tmp_path)
    findings = scan_dead_links(cards, index_dir=memory)
    # ghost-card is dead; real-card resolves; the daily log is not scanned.
    assert [f.link for f in findings] == ["ghost-card"]


def test_index_link_resolves_against_cards_dir(tmp_path):
    """MEMORY.md lives beside cards/, but its links point at cards."""
    from memory_doctor.lint import scan_dead_links

    memory, cards = _split_store(tmp_path)
    (memory / "MEMORY.md").write_text("- [Ghost](ghost.md)\n", encoding="utf-8")
    findings = scan_dead_links(cards, index_dir=memory)
    # index findings carry the raw markdown target, not the slug
    assert any(f.link == "ghost.md" and f.kind == "index" for f in findings)


def test_flat_layout_is_unchanged_when_index_dir_omitted(tmp_path):
    from memory_doctor.lint import scan_dead_links

    flat = tmp_path / "memory"
    flat.mkdir()
    (flat / "a.md").write_text("[[missing]]\n", encoding="utf-8")
    (flat / "MEMORY.md").write_text("index\n", encoding="utf-8")
    assert [f.link for f in scan_dead_links(flat)] == ["missing"]
