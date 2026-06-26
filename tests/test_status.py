import json
from pathlib import Path

from tests.conftest import write_card, write_handoff, write_memory_index
from memory_doctor.paths import PathConfig
from memory_doctor.status import collect_status, format_status_human, format_status_json


def make_cfg(
    memory_dir: Path,
    handoffs_dir: Path,
    max_lines: int = 180,
    max_bytes: int = 24000,
) -> PathConfig:
    return PathConfig(
        memory_dir=memory_dir,
        handoffs_dir=handoffs_dir,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


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


def test_over_bytes_flips_at_boundary(memory_dir, handoffs_dir):
    # One byte under the limit is ok; one byte over flips over_bytes.
    write_memory_index(memory_dir, ["x" * 99])  # 99 'x' + newline = 100 bytes
    s_under = collect_status(make_cfg(memory_dir, handoffs_dir, max_bytes=100))
    assert s_under.memory_index_bytes == 100
    assert s_under.over_bytes is False
    s_over = collect_status(make_cfg(memory_dir, handoffs_dir, max_bytes=99))
    assert s_over.over_bytes is True
    assert s_over.max_bytes == 99


def test_human_format_includes_byte_threshold_line(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["x" * 99])  # 100 bytes
    s = collect_status(make_cfg(memory_dir, handoffs_dir, max_bytes=99))
    out = format_status_human(s)
    assert "bytes:" in out
    assert "100" in out and "99" in out
    assert "OVER" in out


def test_json_shape_contains_all_fields(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, ["x"])
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    payload = json.loads(format_status_json(s))
    for key in [
        "memory_dir", "handoffs_dir", "cards", "memory_index_lines",
        "memory_index_bytes", "pending_handoffs", "processed_handoffs",
        "dead_links", "oldest_pending_age_days", "over_threshold", "max_lines",
        "over_bytes", "max_bytes",
    ]:
        assert key in payload


def test_human_format_does_not_crash_on_empty(memory_dir, handoffs_dir):
    write_memory_index(memory_dir, [])
    s = collect_status(make_cfg(memory_dir, handoffs_dir))
    out = format_status_human(s)
    assert "memory dir" in out.lower() or "cards" in out.lower()
