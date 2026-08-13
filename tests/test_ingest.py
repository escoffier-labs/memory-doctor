import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import write_handoff
from memory_doctor.paths import PathConfig
from memory_doctor.ingest import _plan_targets, run
from memory_doctor.parsing import MAX_HANDOFF_BYTES, MAX_SUGGESTED_CONTENT_BYTES
from memory_doctor.safety import atomic_write_text


def cfg(memory_dir, handoffs_dir):
    return PathConfig(memory_dir=memory_dir, handoffs_dir=handoffs_dir, max_lines=180)


def _parent_snapshot(memory_dir: Path) -> set[tuple[str, str]]:
    """Record sibling names and kinds without reading transaction internals."""
    return {
        (path.name, "dir" if path.is_dir() else "file")
        for path in memory_dir.parent.iterdir()
    }


def test_plan_targets_preserves_validated_submitted_spelling(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import ingest as ingest_mod

    pending = [
        write_handoff(
            handoffs_dir,
            "first.md",
            action="update-card",
            target="Foo.md",
        ),
        write_handoff(
            handoffs_dir,
            "second.md",
            action="update-card",
            target="foo.md",
        ),
    ]
    canonical = memory_dir / "Foo.md"
    monkeypatch.setattr(
        ingest_mod,
        "resolve_card_target",
        lambda memory_root, raw: canonical,
    )

    assert [path.name for path in _plan_targets(pending, memory_dir)] == [
        "Foo.md",
        "foo.md",
    ]


def test_update_card_reads_existing_content_as_utf8(
    memory_dir, handoffs_dir, monkeypatch
):
    card = memory_dir / "unicode.md"
    card.write_text("café\n", encoding="utf-8")
    write_handoff(
        handoffs_dir,
        "unicode.md",
        action="update-card",
        target="unicode.md",
        content="résumé",
    )
    real_read_text = Path.read_text

    def require_utf8(path: Path, *args, **kwargs):
        if path == card:
            assert kwargs.get("encoding") == "utf-8"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", require_utf8)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0
    assert card.read_text(encoding="utf-8").endswith("résumé\n")


def test_empty_apply_creates_no_transaction_state(memory_dir, handoffs_dir, capsys):
    before = _parent_snapshot(memory_dir)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0

    assert "no pending handoffs" in capsys.readouterr().out
    assert _parent_snapshot(memory_dir) == before


@pytest.mark.parametrize(
    ("unsafe_location", "file_attributes", "zero_inode"),
    [
        ("target", 0x1, False),
        ("target", 0x400, False),
        ("target", 0, True),
        ("source", 0x1, False),
        ("source", 0x400, False),
        ("source", 0, True),
    ],
)
def test_apply_preflights_every_artifact_before_move_or_state(
    memory_dir,
    handoffs_dir,
    monkeypatch,
    unsafe_location,
    file_attributes,
    zero_inode,
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    source = write_handoff(
        handoffs_dir,
        "unsafe.md",
        action="update-card",
        target="existing.md",
        content="new content",
    )
    parent_before = _parent_snapshot(memory_dir)
    real_lstat = Path.lstat
    unsafe_path = card if unsafe_location == "target" else source

    def mark_unsafe(path: Path):
        result = real_lstat(path)
        if path == unsafe_path:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=0 if zero_inode else result.st_ino,
                st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns,
                st_file_attributes=file_attributes,
                st_reparse_tag=1 if file_attributes == 0x400 else 0,
            )
        return result

    monkeypatch.setattr(transaction_mod.os, "name", "nt")
    monkeypatch.setattr(Path, "lstat", mark_unsafe)

    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=True)

    assert code == 2
    assert card.read_text() == "original\n"
    assert source.exists()
    assert not (handoffs_dir / "processed" / source.name).exists()
    assert _parent_snapshot(memory_dir) == parent_before


@pytest.mark.parametrize(
    ("first_name", "alias_name"),
    [
        ("Foo.md", "foo.md"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}.md", "cafe\N{COMBINING ACUTE ACCENT}.md"),
    ],
)
def test_apply_rejects_normalized_target_aliases_before_move_or_state(
    memory_dir, handoffs_dir, capsys, first_name, alias_name
):
    card = memory_dir / first_name
    card.write_text("original\n")
    handoffs = [
        write_handoff(
            handoffs_dir,
            "first-alias.md",
            action="update-card",
            target=first_name,
            content="first update",
        ),
        write_handoff(
            handoffs_dir,
            "second-alias.md",
            action="update-card",
            target=alias_name,
            content="second update",
        ),
    ]
    before = _parent_snapshot(memory_dir)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2

    assert "may alias one filesystem entry" in capsys.readouterr().err
    assert card.read_text() == "original\n"
    assert all(path.exists() for path in handoffs)
    assert not any((handoffs_dir / "processed").iterdir())
    assert _parent_snapshot(memory_dir) == before


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
    with patch(
        "memory_doctor.transaction.atomic_write_text", wraps=atomic_write_text
    ) as spy:
        code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert spy.called
    # Target path should be inside memory_dir.
    target = memory_dir / "atomic-card.md"
    assert any(call.args[0] == target for call in spy.call_args_list)


def test_ingest_update_uses_atomic_write(memory_dir, handoffs_dir):
    # update-card must also route through atomic_write_text.
    (memory_dir / "growing-atomic.md").write_text("original\n")
    write_handoff(handoffs_dir, "atomic-update.md", action="update-card",
                  target="growing-atomic.md", content="appended")
    with patch(
        "memory_doctor.transaction.atomic_write_text", wraps=atomic_write_text
    ) as spy:
        code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert spy.called
    # Payload should include both the existing content and the appended block,
    # confirming we compute the combined result before writing (no in-place append).
    target = memory_dir / "growing-atomic.md"
    payload = next(
        call.args[1] for call in spy.call_args_list if call.args[0] == target
    )
    assert "original" in payload
    assert "appended" in payload


def test_oversized_handoff_does_not_overwrite_create_target(memory_dir, handoffs_dir, capsys):
    target = memory_dir / "existing.md"
    target.write_text("sentinel\n")
    handoff = handoffs_dir / "oversized-create.md"
    write_handoff(
        handoffs_dir,
        handoff.name,
        action="create-card",
        target=target.name,
        content="x" * MAX_HANDOFF_BYTES,
    )

    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=True)

    assert code == 1
    assert f"{MAX_HANDOFF_BYTES} byte limit" in capsys.readouterr().out
    assert target.read_text() == "sentinel\n"
    assert handoff.exists()
    assert not (handoffs_dir / "processed" / handoff.name).exists()


def test_oversized_suggested_content_does_not_append_target(memory_dir, handoffs_dir, capsys):
    target = memory_dir / "growing.md"
    target.write_text("sentinel\n")
    handoff = handoffs_dir / "oversized-update.md"
    write_handoff(
        handoffs_dir,
        handoff.name,
        action="update-card",
        target=target.name,
        content="x" * (MAX_SUGGESTED_CONTENT_BYTES + 1),
    )

    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)

    assert code == 1
    assert f"{MAX_SUGGESTED_CONTENT_BYTES} byte limit" in capsys.readouterr().out
    assert target.read_text() == "sentinel\n"
    assert handoff.exists()
    assert not (handoffs_dir / "processed" / handoff.name).exists()


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


# --- reserved targets ---

def test_ingest_rejects_memory_index_target(memory_dir, handoffs_dir, capsys):
    write_handoff(handoffs_dir, "h-evil.md", action="create-card",
                  target="MEMORY.md", content="injected content")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=True)
    assert code == 1
    assert not (memory_dir / "MEMORY.md").exists()
    assert "unsafe target" in capsys.readouterr().out
    assert (handoffs_dir / "h-evil.md").exists()


# --- dirty-tree protection on plain --apply ---

def _commit_all(memory_dir):
    subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(memory_dir), "commit", "--quiet", "-m", "setup"],
        check=True,
    )


def test_plain_apply_refuses_dirty_target_card(git_memory_dir, handoffs_dir, capsys):
    memory_dir = git_memory_dir
    (memory_dir / "existing.md").write_text("original\n")
    _commit_all(memory_dir)
    (memory_dir / "existing.md").write_text("original\nlocal uncommitted edit\n")
    write_handoff(handoffs_dir, "h-dirty.md", action="update-card",
                  target="existing.md", content="appended")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 2
    assert "refusing to apply" in capsys.readouterr().err
    assert "local uncommitted edit" in (memory_dir / "existing.md").read_text()
    assert (handoffs_dir / "h-dirty.md").exists()


def test_repeated_cards_prefix_cannot_bypass_dirty_target_preflight(
    git_memory_dir, handoffs_dir
):
    card = git_memory_dir / "dirty.md"
    card.write_text("original\n", encoding="utf-8")
    _commit_all(git_memory_dir)
    card.write_text("original\noperator edit\n", encoding="utf-8")
    handoff = write_handoff(
        handoffs_dir,
        "repeated-prefix.md",
        action="update-card",
        target="cards/cards/dirty.md",
        content="transaction append",
    )
    before = card.read_bytes()

    assert run(cfg(git_memory_dir, handoffs_dir), apply=True) == 2

    assert card.read_bytes() == before
    assert handoff.exists()


def test_plain_apply_proceeds_on_clean_tree(git_memory_dir, handoffs_dir):
    memory_dir = git_memory_dir
    (memory_dir / "existing.md").write_text("original\n")
    _commit_all(memory_dir)
    write_handoff(handoffs_dir, "h-clean.md", action="update-card",
                  target="existing.md", content="appended")
    code = run(cfg(memory_dir, handoffs_dir), apply=True, force=False)
    assert code == 0
    assert "appended" in (memory_dir / "existing.md").read_text()


def test_plain_apply_reports_git_status_failure(
    git_memory_dir, handoffs_dir, capsys, monkeypatch
):
    from memory_doctor.git import GitStatusError

    memory_dir = git_memory_dir
    (memory_dir / "existing.md").write_text("original\n")
    _commit_all(memory_dir)
    handoff = write_handoff(
        handoffs_dir,
        "h-status-failure.md",
        action="update-card",
        target="existing.md",
        content="appended",
    )

    def fail_status(*args, **kwargs):
        raise GitStatusError("fatal: status exploded")

    monkeypatch.setattr("memory_doctor.ingest.files_have_uncommitted_changes", fail_status)

    code = run(cfg(memory_dir, handoffs_dir), apply=True)

    assert code == 2
    assert "fatal: status exploded" in capsys.readouterr().err
    assert (memory_dir / "existing.md").read_text() == "original\n"
    assert handoff.exists()


def test_ingest_rolls_back_earlier_write_when_later_processed_move_fails(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    first_handoff = write_handoff(
        handoffs_dir,
        "a-first-update.md",
        action="update-card",
        target="existing.md",
        content="first append",
    )
    second_handoff = write_handoff(
        handoffs_dir,
        "b-move-failure.md",
        action="update-card",
        target="existing.md",
        content="second append",
    )
    real_move = transaction_mod.ApplyTransaction.move_handoff
    move_count = 0

    def fail_second_move(transaction, src, dst, **kwargs):
        nonlocal move_count
        move_count += 1
        if move_count == 2:
            raise OSError("move exploded")
        return real_move(transaction, src, dst, **kwargs)

    monkeypatch.setattr(
        transaction_mod.ApplyTransaction, "move_handoff", fail_second_move
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 1
    assert card.read_text() == "original\n"
    assert first_handoff.exists()
    assert second_handoff.exists()
    assert not (handoffs_dir / "processed" / first_handoff.name).exists()
    assert not (handoffs_dir / "processed" / second_handoff.name).exists()
    assert "rolled back" in capsys.readouterr().err

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0
    assert card.read_text().count("first append") == 1
    assert card.read_text().count("second append") == 1


def test_ingest_no_work_fast_path_never_applies_concurrently_arriving_handoff(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import ingest as ingest_mod

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    real_probe = ingest_mod.has_pending_transaction_recovery
    probe_count = 0

    def add_handoff_after_final_probe(path: Path) -> bool:
        nonlocal probe_count
        result = real_probe(path)
        probe_count += 1
        if probe_count == 2:
            write_handoff(
                handoffs_dir,
                "arrived-after-probe.md",
                action="update-card",
                target=card.name,
                content="must wait for the next apply",
            )
        return result

    monkeypatch.setattr(
        ingest_mod,
        "has_pending_transaction_recovery",
        add_handoff_after_final_probe,
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0
    assert "no pending handoffs" in capsys.readouterr().out
    assert card.read_text() == "original\n"
    assert (handoffs_dir / "arrived-after-probe.md").exists()
    assert not (handoffs_dir / "processed" / "arrived-after-probe.md").exists()


def test_ingest_restart_recovers_quarantined_source_before_no_work_return(
    memory_dir, handoffs_dir, capsys
):
    from memory_doctor.transaction import ApplyTransaction

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "h-crashed-after-move.md",
        action="update-card",
        target="existing.md",
        content="recovered update",
    )
    destination = handoffs_dir / "processed" / handoff.name
    abandoned = ApplyTransaction(memory_dir, handoffs_dir)
    abandoned.__enter__()
    abandoned.move_handoff(handoff, destination)
    source_quarantine = abandoned._moves[0].source_quarantine
    journal = abandoned._journal_path
    abandoned._release_lock()

    assert not handoff.exists()
    assert destination.exists()
    assert source_quarantine.exists()
    assert journal.exists()

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0

    assert "recovered an interrupted apply transaction" in capsys.readouterr().err
    assert not handoff.exists()
    assert destination.exists()
    assert card.read_text().startswith("original\n")
    assert card.read_text().count("recovered update") == 1
    assert not source_quarantine.exists()
    assert not journal.exists()


def test_ingest_rechecks_recovery_after_concurrent_handoff_quarantine(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import ingest as ingest_mod
    from memory_doctor.transaction import ApplyTransaction

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "h-raced-after-probe.md",
        action="update-card",
        target="existing.md",
        content="raced recovery",
    )
    destination = handoffs_dir / "processed" / handoff.name
    real_probe = ingest_mod.has_pending_transaction_recovery
    raced = False

    def crash_after_first_probe(path: Path) -> bool:
        nonlocal raced
        result = real_probe(path)
        if not raced:
            raced = True
            abandoned = ApplyTransaction(memory_dir, handoffs_dir)
            abandoned.__enter__()
            abandoned.move_handoff(handoff, destination)
            abandoned._release_lock()
        return result

    monkeypatch.setattr(
        ingest_mod, "has_pending_transaction_recovery", crash_after_first_probe
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0
    assert destination.exists()
    assert card.read_text().count("raced recovery") == 1


def test_ingest_refuses_handoff_replaced_after_transaction_parse(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import ingest as ingest_mod

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "h-operator-replaced.md",
        action="update-card",
        target="existing.md",
        content="stale parsed content",
    )
    replacement = write_handoff(
        handoffs_dir,
        "replacement.tmp.md",
        action="update-card",
        target="existing.md",
        content="operator replacement",
    )
    replacement_bytes = replacement.read_bytes()
    replacement.unlink()
    real_parse = ingest_mod.parse_handoff
    parse_count = 0

    def replace_after_locked_parse(path: Path):
        nonlocal parse_count
        parsed = real_parse(path)
        if path == handoff:
            parse_count += 1
            if parse_count == 3:
                operator_path = handoffs_dir / "operator.tmp"
                operator_path.write_bytes(replacement_bytes)
                operator_path.replace(handoff)
        return parsed

    monkeypatch.setattr(ingest_mod, "parse_handoff", replace_after_locked_parse)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    assert handoff.read_bytes() == replacement_bytes
    assert not (handoffs_dir / "processed" / handoff.name).exists()
    assert card.read_text() == "original\n"


def test_ingest_preserves_card_replaced_after_read_before_write(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "existing.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "h-card-replaced.md",
        action="update-card",
        target="existing.md",
        content="stale append",
    )
    real_write = transaction_mod.ApplyTransaction.write_text
    replaced = False

    def replace_before_transaction_snapshot(transaction, path, content, **kwargs):
        nonlocal replaced
        if path == card and not replaced:
            replaced = True
            operator_path = memory_dir / "operator.tmp"
            operator_path.write_text("operator replacement\n")
            operator_path.replace(card)
        return real_write(transaction, path, content, **kwargs)

    monkeypatch.setattr(
        transaction_mod.ApplyTransaction,
        "write_text",
        replace_before_transaction_snapshot,
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    assert card.read_text() == "operator replacement\n"
    assert handoff.exists()
    assert not (handoffs_dir / "processed" / handoff.name).exists()


def test_ingest_syncs_move_directories_before_clearing_journal(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import safety as safety_mod
    from memory_doctor import transaction as transaction_mod

    handoff = write_handoff(
        handoffs_dir,
        "h-durable-move.md",
        action="no-card",
        target="unused.md",
        content="no card",
    )
    destination = handoffs_dir / "processed" / handoff.name
    calls: list[str] = []
    real_link = transaction_mod.os.link
    real_rename = transaction_mod._rename_noreplace
    real_unlink = Path.unlink
    real_fsync = transaction_mod.os.fsync
    processed_identity = (
        destination.parent.stat().st_dev,
        destination.parent.stat().st_ino,
    )

    def record_link(source, target, **kwargs):
        calls.append("link")
        return real_link(source, target, **kwargs)

    def record_rename(source, target):
        calls.append("rename-source-quarantine")
        return real_rename(source, target)

    def record_directory_sync(path: Path) -> None:
        if path.name.startswith(".memory-doctor-"):
            calls.append("fsync:state")
        else:
            calls.append(f"fsync:{path.name}")

    def record_fd_sync(fd: int) -> None:
        opened = transaction_mod.os.fstat(fd)
        if (opened.st_dev, opened.st_ino) == processed_identity:
            calls.append("fsync:processed")
        return real_fsync(fd)

    def record_unlink(path: Path, *args, **kwargs):
        if path.name.startswith(".mds-"):
            calls.append("unlink-source-quarantine")
        elif path.name == "apply.journal.json":
            calls.append("unlink:journal")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(transaction_mod.os, "link", record_link)
    monkeypatch.setattr(transaction_mod.os, "fsync", record_fd_sync)
    monkeypatch.setattr(transaction_mod, "_rename_noreplace", record_rename)
    monkeypatch.setattr(
        transaction_mod, "_fsync_directory", record_directory_sync, raising=False
    )
    monkeypatch.setattr(safety_mod, "_fsync_directory", record_directory_sync)
    monkeypatch.setattr(Path, "unlink", record_unlink)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0
    assert destination.exists()
    assert calls.index("link") < calls.index("fsync:processed")
    assert calls.index("rename-source-quarantine") < calls.index(
        f"fsync:{handoffs_dir.name}"
    )
    unlink_quarantine = calls.index("unlink-source-quarantine")
    assert calls.index(f"fsync:{handoffs_dir.name}", unlink_quarantine) < calls.index(
        "unlink:journal"
    )
    assert calls.index("unlink:journal") < len(calls) - 1
    assert calls[-1] == "fsync:state"


def test_ingest_refuses_processed_name_collision_before_write(
    memory_dir, handoffs_dir, capsys
):
    card = memory_dir / "existing.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "same-name.md",
        action="update-card",
        target="existing.md",
        content="must not append",
    )
    (handoffs_dir / "processed" / handoff.name).write_text("older handoff\n")

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    assert card.read_text() == "original\n"
    assert handoff.exists()
    assert "processed" in capsys.readouterr().err


def test_ingest_refuses_dangling_processed_name_collision_before_state(
    memory_dir, handoffs_dir, capsys
):
    handoff = write_handoff(
        handoffs_dir,
        "dangling-collision.md",
        action="create-card",
        target="never-written.md",
        content="must not write",
    )
    collision = handoffs_dir / "processed" / handoff.name
    collision.symlink_to("missing-handoff.md")
    before = _parent_snapshot(memory_dir)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2

    assert _parent_snapshot(memory_dir) == before
    assert handoff.exists()
    assert collision.is_symlink()
    assert not (memory_dir / "never-written.md").exists()
    assert "processed" in capsys.readouterr().err


def test_identical_create_tracks_card_until_transaction_commit(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "existing.md"
    card.write_text("same content\n", encoding="utf-8")
    handoff = write_handoff(
        handoffs_dir,
        "identical.md",
        action="create-card",
        target=card.name,
        content="same content",
    )
    real_commit = transaction_mod.ApplyTransaction.commit

    def replace_card_before_commit(transaction):
        replacement = memory_dir / "operator.tmp"
        replacement.write_text("operator replacement\n", encoding="utf-8")
        replacement.replace(card)
        return real_commit(transaction)

    monkeypatch.setattr(
        transaction_mod.ApplyTransaction,
        "commit",
        replace_card_before_commit,
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    assert card.read_text(encoding="utf-8") == "operator replacement\n"
    assert handoff.exists()
    assert not (handoffs_dir / "processed" / handoff.name).exists()


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_ingest_transaction_construction_failure_is_handled(
    memory_dir, handoffs_dir, monkeypatch, capsys, error_type
):
    handoff = write_handoff(
        handoffs_dir,
        "constructor-failure.md",
        action="create-card",
        target="never-written.md",
        content="must stay pending",
    )
    before = _parent_snapshot(memory_dir)

    def fail_construction(*args, **kwargs):
        raise error_type("cannot stat memory root")

    monkeypatch.setattr("memory_doctor.ingest.ApplyTransaction", fail_construction)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    err = capsys.readouterr().err
    assert "transaction recovery incomplete" in err
    assert "cannot stat memory root" in err
    assert "Traceback" not in err
    assert _parent_snapshot(memory_dir) == before
    assert handoff.exists()
    assert not (memory_dir / "never-written.md").exists()


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_ingest_transaction_entry_failure_is_handled(
    memory_dir, handoffs_dir, monkeypatch, capsys, error_type
):
    handoff = write_handoff(
        handoffs_dir,
        "entry-failure.md",
        action="create-card",
        target="never-written.md",
        content="must stay pending",
    )
    before = _parent_snapshot(memory_dir)

    def fail_entry(transaction):
        raise error_type("cannot create transaction lock")

    monkeypatch.setattr(
        "memory_doctor.transaction.ApplyTransaction.__enter__", fail_entry
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    err = capsys.readouterr().err
    assert "transaction recovery incomplete" in err
    assert "cannot create transaction lock" in err
    assert "Traceback" not in err
    assert _parent_snapshot(memory_dir) == before
    assert handoff.exists()
    assert not (memory_dir / "never-written.md").exists()


def test_ingest_invalid_author_preflight_creates_no_transaction_state(
    git_memory_dir, handoffs_dir
):
    handoff = write_handoff(
        handoffs_dir,
        "invalid-author-no-state.md",
        action="create-card",
        target="never-written.md",
        content="must stay pending",
    )
    before = _parent_snapshot(git_memory_dir)

    assert run(
        cfg(git_memory_dir, handoffs_dir),
        apply=True,
        commit=True,
        commit_author="bad-author",
    ) == 2

    assert _parent_snapshot(git_memory_dir) == before
    assert handoff.exists()
    assert not (git_memory_dir / "never-written.md").exists()


def test_ingest_non_git_commit_preflight_creates_no_transaction_state(
    memory_dir, handoffs_dir
):
    handoff = write_handoff(
        handoffs_dir,
        "non-git-no-state.md",
        action="create-card",
        target="never-written.md",
        content="must stay pending",
    )
    before = _parent_snapshot(memory_dir)

    assert run(cfg(memory_dir, handoffs_dir), apply=True, commit=True) == 2

    assert _parent_snapshot(memory_dir) == before
    assert handoff.exists()
    assert not (memory_dir / "never-written.md").exists()


def test_ingest_dirty_tree_preflight_creates_no_transaction_state(
    git_memory_dir, handoffs_dir
):
    card = git_memory_dir / "existing-no-state.md"
    card.write_text("original\n")
    _commit_all(git_memory_dir)
    card.write_text("operator edit\n")
    handoff = write_handoff(
        handoffs_dir,
        "dirty-no-state.md",
        action="update-card",
        target=card.name,
        content="must not append",
    )
    before = _parent_snapshot(git_memory_dir)

    assert run(cfg(git_memory_dir, handoffs_dir), apply=True) == 2

    assert _parent_snapshot(git_memory_dir) == before
    assert card.read_text() == "operator edit\n"
    assert handoff.exists()


def test_ingest_revalidates_git_status_under_transaction_lock(
    git_memory_dir, handoffs_dir, monkeypatch
):
    card = git_memory_dir / "race.md"
    card.write_text("original\n")
    _commit_all(git_memory_dir)
    handoff = write_handoff(
        handoffs_dir,
        "race.md",
        action="update-card",
        target=card.name,
        content="must not append",
    )
    calls = 0

    def clean_then_dirty(memory_dir, paths):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [(card, " M")]

    monkeypatch.setattr(
        "memory_doctor.ingest.files_have_uncommitted_changes", clean_then_dirty
    )

    assert run(cfg(git_memory_dir, handoffs_dir), apply=True) == 2
    assert calls == 2
    assert card.read_text() == "original\n"
    assert handoff.exists()


def test_ingest_unsupported_move_fails_before_card_write(
    memory_dir, tmp_path, monkeypatch, capsys
):
    from memory_doctor.transaction import TransactionRecoveryError

    handoffs_dir = tmp_path / "unsupported-handoffs"
    handoffs_dir.mkdir()
    card = memory_dir / "existing-unsupported.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "unsupported.md",
        action="update-card",
        target=card.name,
        content="must not append",
    )

    def unsupported_platform(*args, **kwargs):
        raise TransactionRecoveryError("hard links are unsupported")

    monkeypatch.setattr(
        "memory_doctor.ingest.preflight_transaction_capabilities",
        unsupported_platform,
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 2
    assert "hard links are unsupported" in capsys.readouterr().err
    assert card.read_text() == "original\n"
    assert handoff.exists()
    assert not (handoffs_dir / "processed").exists()


def test_ingest_runtime_link_failure_happens_before_card_write(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import transaction as transaction_mod

    card = memory_dir / "existing-link-failure.md"
    card.write_text("original\n")
    handoff = write_handoff(
        handoffs_dir,
        "link-failure.md",
        action="update-card",
        target=card.name,
        content="must not append",
    )
    real_link = transaction_mod.os.link

    def fail_link(source, destination, **kwargs):
        if Path(source) == handoff:
            raise OSError("link failed")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(transaction_mod.os, "link", fail_link)

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 1
    assert "cannot link handoff" in capsys.readouterr().err
    assert card.read_text() == "original\n"
    assert handoff.exists()
    assert not (handoffs_dir / "processed" / handoff.name).exists()


def test_ingest_rejects_overlapping_memory_and_handoffs_before_state_or_writes(
    tmp_path, capsys
):
    shared = tmp_path / "shared"
    shared.mkdir()
    handoff = write_handoff(
        shared,
        "pending.md",
        action="create-card",
        target="pending.md",
        content="replacement card content",
    )
    original = handoff.read_bytes()

    assert run(cfg(shared, shared), apply=True, force=True) == 2

    assert "target overlaps" in capsys.readouterr().err
    assert handoff.read_bytes() == original
    assert not (shared / "processed").exists()
    assert not list(tmp_path.glob(".memory-doctor-*"))


def test_ingest_supports_nested_distinct_handoffs_root(tmp_path):
    memory_dir = tmp_path / "memory"
    handoffs_dir = memory_dir / "handoffs"
    memory_dir.mkdir()
    handoffs_dir.mkdir()
    handoff = write_handoff(
        handoffs_dir,
        "nested.md",
        action="create-card",
        target="nested-card.md",
        content="nested configuration works",
    )

    assert run(cfg(memory_dir, handoffs_dir), apply=True) == 0

    assert (memory_dir / "nested-card.md").read_text() == (
        "nested configuration works\n"
    )
    assert not handoff.exists()
    assert (handoffs_dir / "processed" / handoff.name).exists()


def test_ingest_rejects_nested_card_and_processed_handoff_path_overlap(
    tmp_path, capsys
):
    handoffs_dir = tmp_path / "handoffs"
    memory_dir = handoffs_dir / "processed"
    handoffs_dir.mkdir()
    memory_dir.mkdir()
    handoff = write_handoff(
        handoffs_dir,
        "pending.md",
        action="create-card",
        target="pending.md",
        content="must not replace the handoff",
    )
    original = handoff.read_bytes()

    assert run(cfg(memory_dir, handoffs_dir), apply=True, force=True) == 2

    assert "target overlaps" in capsys.readouterr().err
    assert handoff.read_bytes() == original
    assert not (memory_dir / handoff.name).exists()
    assert not list(handoffs_dir.glob(".memory-doctor-*"))


def test_template_is_not_a_pending_handoff(tmp_path):
    """TEMPLATE.md documents the format; counting it invents a stuck handoff.

    On the Rocinante fleet this showed as "1 pending, oldest 114.8 days" for
    the entire life of the directory, and every ingest run printed a parse
    error for it.
    """
    from memory_doctor.paths import iter_pending_handoffs

    handoffs = tmp_path / "memory-handoffs"
    handoffs.mkdir()
    (handoffs / "TEMPLATE.md").write_text("# Memory Handoff\n", encoding="utf-8")
    (handoffs / "real-handoff.md").write_text("# Memory Handoff\n", encoding="utf-8")

    pending = iter_pending_handoffs(handoffs)
    assert [p.name for p in pending] == ["real-handoff.md"]


def test_template_match_is_case_insensitive(tmp_path):
    from memory_doctor.paths import iter_pending_handoffs

    handoffs = tmp_path / "memory-handoffs"
    handoffs.mkdir()
    (handoffs / "Template.md").write_text("x", encoding="utf-8")
    assert iter_pending_handoffs(handoffs) == []


def test_empty_inbox_is_empty(tmp_path):
    from memory_doctor.paths import iter_pending_handoffs

    handoffs = tmp_path / "memory-handoffs"
    handoffs.mkdir()
    assert iter_pending_handoffs(handoffs) == []
