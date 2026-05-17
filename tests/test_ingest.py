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


def test_ingest_rejects_path_traversal_target(memory_dir, handoffs_dir, tmp_path):
    # Pre-create a sentinel file outside memory_dir that the malicious handoff
    # would clobber if traversal were not blocked.
    outside = tmp_path / "outside.md"
    outside.write_text("untouched")
    write_handoff(handoffs_dir, "evil.md", action="create-card",
                  target="../outside.md", content="malicious payload")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=True)
    assert code == 1
    # Sentinel untouched, handoff still in inbox (not promoted to processed).
    assert outside.read_text() == "untouched"
    assert (handoffs_dir / "evil.md").exists()
    assert not (handoffs_dir / "processed" / "evil.md").exists()


def test_multi_handoff_batch(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "b-1.md", action="create-card", target="a.md", content="a-body")
    write_handoff(handoffs_dir, "b-2.md", action="create-card", target="b.md", content="b-body")
    write_handoff(handoffs_dir, "b-3.md", action="no-card", target="x.md", content="x")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert (memory_dir / "a.md").exists()
    assert (memory_dir / "b.md").exists()
    assert len(list((handoffs_dir / "processed").glob("*.md"))) == 3
