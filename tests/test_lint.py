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
