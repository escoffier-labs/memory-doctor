import subprocess
from pathlib import Path
from unittest.mock import patch

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


def test_ingest_autocreates_processed_subdir(memory_dir, tmp_path):
    # Build a handoffs dir WITHOUT a pre-existing processed/ subdir.
    handoffs_dir = tmp_path / "handoffs-fresh"
    handoffs_dir.mkdir()
    write_handoff(handoffs_dir, "h-fresh.md", action="create-card",
                  target="fresh.md", content="fresh body")
    assert not (handoffs_dir / "processed").exists()
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert (handoffs_dir / "processed").is_dir()
    assert (handoffs_dir / "processed" / "h-fresh.md").exists()


def test_ingest_create_uses_atomic_write(memory_dir, handoffs_dir):
    # create-card must route through atomic_write_text (crash-safe writer),
    # not a raw target.write_text(...). Asserted via spy on the imported name.
    write_handoff(handoffs_dir, "atomic-create.md", action="create-card",
                  target="atomic-card.md", content="atomic body")
    with patch("memory_doctor.ingest.atomic_write_text") as spy:
        code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert spy.called
    # Target path should be inside memory_dir.
    target_arg = spy.call_args.args[0]
    assert target_arg == memory_dir / "atomic-card.md"


def test_ingest_update_uses_atomic_write(memory_dir, handoffs_dir):
    # update-card must also route through atomic_write_text.
    (memory_dir / "growing-atomic.md").write_text("original\n")
    write_handoff(handoffs_dir, "atomic-update.md", action="update-card",
                  target="growing-atomic.md", content="appended")
    with patch("memory_doctor.ingest.atomic_write_text") as spy:
        code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert spy.called
    # Payload should include both the existing content and the appended block,
    # confirming we compute the combined result before writing (no in-place append).
    payload = spy.call_args.args[1]
    assert "original" in payload
    assert "appended" in payload


def test_multi_handoff_batch(memory_dir, handoffs_dir):
    write_handoff(handoffs_dir, "b-1.md", action="create-card", target="a.md", content="a-body")
    write_handoff(handoffs_dir, "b-2.md", action="create-card", target="b.md", content="b-body")
    write_handoff(handoffs_dir, "b-3.md", action="no-card", target="x.md", content="x")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert (memory_dir / "a.md").exists()
    assert (memory_dir / "b.md").exists()
    assert len(list((handoffs_dir / "processed").glob("*.md"))) == 3


def test_ingest_commit_creates_one_commit(git_memory_dir, handoffs_dir):
    # Use the existing handoff seeding pattern in this test module.
    # (See other tests in this file for the create-card handoff template format.)
    handoff = handoffs_dir / "h-commit-test.md"
    handoff.write_text(
        "# Handoff\n\n"
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\ncard-commit-test.md\n\n"
        "## Suggested card content\n"
        "---\nname: card-commit-test\n---\n\nbody\n"
    )

    from memory_doctor.ingest import run as ingest_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = ingest_run(cfg, apply=True, commit=True)
    assert rc == 0

    import subprocess
    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    # Baseline commit + one new commit from ingest.
    assert log.count("\n") == 2
    assert "memory-doctor ingest:" in log


def test_ingest_commit_refuses_when_not_git_repo(memory_dir, handoffs_dir):
    handoff = handoffs_dir / "h-norepo.md"
    handoff.write_text(
        "# Handoff\n\n"
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\ncard-norepo.md\n\n"
        "## Suggested card content\nbody\n"
    )
    from memory_doctor.ingest import run as ingest_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = ingest_run(cfg, apply=True, commit=True)
    assert rc == 2
    # File should not have been written.
    assert not (memory_dir / "card-norepo.md").exists()


def test_ingest_commit_invalid_author_refuses_before_writes(git_memory_dir, handoffs_dir):
    handoff = handoffs_dir / "h-invalid-author.md"
    handoff.write_text(
        "# Handoff\n\n"
        "## Recommended memory action\ncreate-card\n\n"
        "## Target card\ncard-invalid-author.md\n\n"
        "## Suggested card content\nbody\n"
    )
    from memory_doctor.ingest import run as ingest_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = ingest_run(cfg, apply=True, commit=True, commit_author="bad-author")
    assert rc == 2
    assert not (git_memory_dir / "card-invalid-author.md").exists()
    assert handoff.exists()
    assert not (handoffs_dir / "processed" / "h-invalid-author.md").exists()
    status = subprocess.run(
        ["git", "-C", str(git_memory_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == ""
