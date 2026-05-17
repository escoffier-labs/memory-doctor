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


def test_apply_is_idempotent_via_marker(memory_dir, handoffs_dir):
    # Re-applying the same flatten plan must NOT duplicate the appended block.
    write_card(memory_dir, "topic-i", "original body\n")
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        "- [topic-i](topic-i.md) - first line",
        "  detail to flatten",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True)
    assert code == 0
    after_first = (memory_dir / "topic-i.md").read_text()
    # Manually re-introduce the same multi-line entry to simulate re-apply.
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        "- [topic-i](topic-i.md) - first line",
        "  detail to flatten",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True)
    assert code == 0
    after_second = (memory_dir / "topic-i.md").read_text()
    # Marker present exactly once; no duplicated "From index" header.
    assert after_second.count("<!-- compact:") == 1
    assert after_second.count("## From index") == 1


def test_compact_skips_unsafe_target(memory_dir, handoffs_dir, tmp_path, capsys):
    # MEMORY.md references a path-traversal target. Plan must skip it AND
    # apply must never write outside memory_dir.
    outside = tmp_path / "outside.md"
    outside.write_text("untouched")
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        "- [evil](../outside.md) - bullet hook",
        "  detail line 1",
        "  detail line 2",
    ])
    plan = plan_compaction(memory_dir, max_lines=2)
    assert "../outside.md" in plan.unsafe_targets
    assert all(f.target_name != "../outside.md" for f in plan.flattens)
    code = run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True)
    # No safe flatten candidates remain, but the unsafe one is reported.
    assert outside.read_text() == "untouched"
    out = capsys.readouterr().out
    assert "unsafe targets" in out.lower()


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
