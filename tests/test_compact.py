from pathlib import Path

from tests.conftest import write_card, write_memory_index
from memory_doctor.paths import PathConfig
from memory_doctor.compact import run, plan_compaction


def cfg(memory_dir, handoffs_dir, max_lines=10, max_bytes=24000, max_hook_chars=140):
    return PathConfig(
        memory_dir=memory_dir,
        handoffs_dir=handoffs_dir,
        max_lines=max_lines,
        max_bytes=max_bytes,
        max_hook_chars=max_hook_chars,
    )


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


def test_same_title_different_detail_produces_distinct_markers(memory_dir, handoffs_dir):
    # Two multi-line entries with the same title + same target + same day but
    # DIFFERENT detail must produce distinct markers, otherwise the second
    # flatten silently skips appending while the index rewrite still strips
    # its detail lines = data loss.
    write_card(memory_dir, "topic-x", "original\n")
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        "- [topic-x](topic-x.md) - first hook",
        "  detail block ONE - unique content here",
        "- [topic-x](topic-x.md) - second hook",
        "  detail block TWO - totally different unique content",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True)
    assert code == 0
    topic = (memory_dir / "topic-x.md").read_text()
    # Both detail blocks should have been appended (two distinct markers).
    assert topic.count("<!-- compact:") == 2
    assert "detail block ONE" in topic
    assert "detail block TWO" in topic
    # And both detail lines should have been stripped from MEMORY.md.
    index = (memory_dir / "MEMORY.md").read_text()
    assert "detail block ONE" not in index
    assert "detail block TWO" not in index


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


def test_compact_commit_creates_one_commit(git_memory_dir, handoffs_dir):
    # Seed MEMORY.md over the threshold with a multi-line entry pointing
    # at a topic file that exists (matches the existing flatten test fixture pattern).
    topic = git_memory_dir / "topic-a.md"
    topic.write_text("# topic-a\n\nbody\n")
    import subprocess
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "add topic-a"],
        check=True,
    )

    # Build a MEMORY.md that exceeds max_lines=5 so compact triggers.
    index = git_memory_dir / "MEMORY.md"
    lines = [
        "# Memory Index",
        "",
        "## Section",
        "- [topic-a](topic-a.md) one-liner hook",
        "  detail-line-1",
        "  detail-line-2",
        "  detail-line-3",
    ]
    index.write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "-C", str(git_memory_dir), "add", str(index)], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "seed MEMORY.md"],
        check=True,
    )

    from memory_doctor.compact import run as compact_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=5)
    rc = compact_run(cfg, apply=True, commit=True)
    assert rc == 0

    log = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "memory-doctor compact:" in log
    # Subject should mention the line-count delta.
    subject = subprocess.run(
        ["git", "-C", str(git_memory_dir), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert "MEMORY.md" in subject and "->" in subject


def test_compact_commit_skipped_when_no_changes(git_memory_dir, handoffs_dir):
    # MEMORY.md under threshold = no flatten = no commit.
    (git_memory_dir / "MEMORY.md").write_text("# tiny\n")
    import subprocess
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "tiny memory"],
        check=True,
    )
    baseline_count = subprocess.run(
        ["git", "-C", str(git_memory_dir), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    from memory_doctor.compact import run as compact_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=180)
    rc = compact_run(cfg, apply=True, commit=True)
    assert rc == 0
    after_count = subprocess.run(
        ["git", "-C", str(git_memory_dir), "rev-list", "--count", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert after_count == baseline_count


def test_compact_commit_invalid_author_refuses_before_writes(git_memory_dir, handoffs_dir):
    import subprocess
    topic = git_memory_dir / "topic-author.md"
    topic.write_text("# topic-author\n\nbody\n")
    index = git_memory_dir / "MEMORY.md"
    index.write_text(
        "# Memory Index\n\n"
        "## Section\n"
        "- [topic-author](topic-author.md) hook\n"
        "  detail-line-1\n"
        "  detail-line-2\n"
    )
    subprocess.run(["git", "-C", str(git_memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_memory_dir), "commit", "--quiet", "-m", "seed compact author case"],
        check=True,
    )

    before_index = index.read_text()
    before_topic = topic.read_text()
    from memory_doctor.compact import run as compact_run
    from memory_doctor.paths import PathConfig
    cfg = PathConfig(memory_dir=git_memory_dir, handoffs_dir=handoffs_dir, max_lines=2)
    rc = compact_run(cfg, apply=True, commit=True, commit_author="bad-author")
    assert rc == 2
    assert index.read_text() == before_index
    assert topic.read_text() == before_topic
    status = subprocess.run(
        ["git", "-C", str(git_memory_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == ""


# --- Change 2: tighten overlong single-line hooks ---------------------------

EMDASH = "—"   # em dash
ENDASH = "–"   # en dash
HBAR = "―"     # horizontal bar
ARROW = "→"    # right arrow
GTE = "≥"      # greater-than-or-equal
LTE = "≤"      # less-than-or-equal
APPROX = "≈"   # almost equal
MIDDOT = "·"   # middle dot


def _link_count(text: str) -> int:
    return text.count("](")


def test_plan_identifies_overlong_single_line_hooks(memory_dir):
    write_card(memory_dir, "topic-long", "## existing\nbody\n")
    long_hook = "this hook is way too long " * 10  # ~260 chars
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [topic-long](topic-long.md) {long_hook}",
        "- [topic-long](topic-long.md) short hook stays",
    ])
    plan = plan_compaction(memory_dir, max_lines=100, max_hook_chars=140)
    assert any(t.target_name == "topic-long.md" for t in plan.tightens)
    # The short-hook entry must NOT be a tighten candidate.
    assert len(plan.tightens) == 1


def test_apply_tightens_long_hook_and_moves_full_text_to_card(memory_dir, handoffs_dir):
    write_card(memory_dir, "topic-long", "original card body\n")
    long_hook = "alpha beta gamma delta epsilon " * 8  # ~248 chars
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [topic-long](topic-long.md) {long_hook.strip()}",
    ])
    before_bytes = (memory_dir / "MEMORY.md").stat().st_size
    code = run(
        cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140),
        apply=True,
    )
    assert code == 0
    card = (memory_dir / "topic-long.md").read_text()
    index = (memory_dir / "MEMORY.md").read_text()
    # Full hook text preserved in the card.
    assert long_hook.strip() in card
    assert "From index" in card
    # Index line shortened with an ellipsis, link prefix intact.
    assert "- [topic-long](topic-long.md) " in index
    assert "..." in index
    assert long_hook.strip() not in index
    # Index got smaller.
    assert (memory_dir / "MEMORY.md").stat().st_size < before_bytes
    # No pointer lost.
    assert _link_count(index) == 1


def test_tighten_normalizes_em_dashes(memory_dir, handoffs_dir):
    write_card(memory_dir, "topic-dash", "body\n")
    long_hook = ("uses an em dash " + EMDASH + " right here and a long tail ") * 6
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [topic-dash](topic-dash.md) {long_hook.strip()}",
    ])
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    index = (memory_dir / "MEMORY.md").read_text()
    for ch in (EMDASH, ENDASH, HBAR, ARROW, GTE, LTE, APPROX, MIDDOT):
        assert ch not in index


def test_em_dash_in_short_entry_normalized_in_place(memory_dir, handoffs_dir):
    # Short entry: not a tighten candidate, but em dash still must be scrubbed.
    write_card(memory_dir, "topic-s", "body\n")
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section " + EMDASH + " heading dash",
        f"- [topic-s](topic-s.md) short hook with {EMDASH} dash",
    ])
    # Over byte threshold so compact triggers even though nothing to flatten/tighten.
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    index = (memory_dir / "MEMORY.md").read_text()
    assert EMDASH not in index
    # The short hook is preserved (not truncated).
    assert "short hook with - dash" in index
    assert _link_count(index) == 1


def test_tighten_leaves_link_targets_untouched(memory_dir, handoffs_dir):
    # A target with characters that could be hit by normalization must survive.
    write_card(memory_dir, "topic-z", "body\n")
    long_hook = "zeta eta theta iota kappa lambda mu nu xi omicron pi rho " * 4
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [topic-z](topic-z.md) {long_hook.strip()}",
    ])
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    index = (memory_dir / "MEMORY.md").read_text()
    assert "(topic-z.md)" in index


def test_tighten_dangling_link_stays_full_only_normalized(memory_dir, handoffs_dir):
    # No card on disk: the index may be the ONLY record. Do not truncate;
    # only normalize the em dash.
    long_hook = ("dangling " + EMDASH + " long hook content here and more ") * 8
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [gone](gone.md) {long_hook.strip()}",
    ])
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    index = (memory_dir / "MEMORY.md").read_text()
    # Full text preserved (the normalized version, without em dash).
    normalized_hook = long_hook.strip().replace(EMDASH, "-")
    assert normalized_hook in index
    assert "..." not in index
    assert EMDASH not in index
    assert _link_count(index) == 1


def test_tighten_skips_unsafe_target(memory_dir, handoffs_dir, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("untouched")
    long_hook = "unsafe path hook content that is quite long indeed " * 5
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [evil](../outside.md) {long_hook.strip()}",
    ])
    plan = plan_compaction(memory_dir, max_lines=100, max_hook_chars=140)
    assert "../outside.md" in plan.unsafe_targets
    assert all(t.target_name != "../outside.md" for t in plan.tightens)
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    assert outside.read_text() == "untouched"


def test_tighten_is_idempotent(memory_dir, handoffs_dir):
    write_card(memory_dir, "topic-idem", "body\n")
    long_hook = "idempotent hook content repeated many times over here " * 5
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [topic-idem](topic-idem.md) {long_hook.strip()}",
    ])
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    index_after_first = (memory_dir / "MEMORY.md").read_text()
    card_after_first = (memory_dir / "topic-idem.md").read_text()
    # Second apply on the already-tightened index must be a no-op.
    run(cfg(memory_dir, handoffs_dir, max_lines=100, max_bytes=1, max_hook_chars=140), apply=True)
    index_after_second = (memory_dir / "MEMORY.md").read_text()
    card_after_second = (memory_dir / "topic-idem.md").read_text()
    assert index_after_second == index_after_first
    assert card_after_second == card_after_first
    assert card_after_second.count("tighten:") == 1


def test_compact_triggers_on_bytes_even_under_line_threshold(memory_dir, handoffs_dir, capsys):
    write_card(memory_dir, "topic-b", "body\n")
    long_hook = "byte trigger hook with lots and lots of repeated content " * 6
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        f"- [topic-b](topic-b.md) {long_hook.strip()}",
    ])
    # Way under the line threshold, but over the byte threshold.
    code = run(
        cfg(memory_dir, handoffs_dir, max_lines=10000, max_bytes=1, max_hook_chars=140),
        apply=True,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "no action needed" not in out.lower()
    index = (memory_dir / "MEMORY.md").read_text()
    assert "..." in index


def test_compact_truly_nothing_to_do_message(memory_dir, handoffs_dir, capsys):
    # Under both thresholds, no flatten, no tighten, no em dash.
    write_memory_index(memory_dir, [
        "# Memory Index",
        "## Section",
        "- [a](a.md) short clean hook",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=10000, max_bytes=10000, max_hook_chars=140), apply=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "no action needed" in out.lower()
