from pathlib import Path
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize(
    ("file_attributes", "zero_inode"),
    [(0x1, False), (0x400, False), (0, True)],
)
def test_apply_preflights_all_targets_before_first_compact_write(
    memory_dir, handoffs_dir, monkeypatch, file_attributes, zero_inode
):
    from memory_doctor import transaction as transaction_mod

    first = write_card(memory_dir, "first", "first original\n")
    second = write_card(memory_dir, "second", "second original\n")
    index = write_memory_index(
        memory_dir,
        [
            "# Memory Index",
            "- [first](first.md) - first hook",
            "  first detail",
            "- [second](second.md) - second hook",
            "  second detail",
        ],
    )
    originals = (first.read_bytes(), second.read_bytes(), index.read_bytes())
    parent_before = {path.name for path in memory_dir.parent.iterdir()}
    real_lstat = Path.lstat

    def mark_second_unsafe(path: Path):
        result = real_lstat(path)
        if path == second:
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
    monkeypatch.setattr(Path, "lstat", mark_second_unsafe)

    code = run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True)

    assert code == 2
    assert (first.read_bytes(), second.read_bytes(), index.read_bytes()) == originals
    assert {path.name for path in memory_dir.parent.iterdir()} == parent_before


@pytest.mark.parametrize(
    ("first_name", "alias_name"),
    [
        ("Foo.md", "foo.md"),
        ("caf\N{LATIN SMALL LETTER E WITH ACUTE}.md", "cafe\N{COMBINING ACUTE ACCENT}.md"),
    ],
)
def test_apply_rejects_normalized_compact_target_aliases_before_write_or_state(
    memory_dir, handoffs_dir, capsys, first_name, alias_name
):
    first = memory_dir / first_name
    alias = memory_dir / alias_name
    first.write_text("first original\n")
    alias.write_text("alias original\n")
    index = write_memory_index(
        memory_dir,
        [
            "# Memory Index",
            f"- [first]({first_name}) - first hook",
            "  first detail",
            f"- [alias]({alias_name}) - alias hook",
            "  alias detail",
        ],
    )
    originals = (first.read_bytes(), alias.read_bytes(), index.read_bytes())
    parent_before = {path.name for path in memory_dir.parent.iterdir()}

    assert run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True) == 2

    assert "may alias one filesystem entry" in capsys.readouterr().err
    assert (first.read_bytes(), alias.read_bytes(), index.read_bytes()) == originals
    assert {path.name for path in memory_dir.parent.iterdir()} == parent_before


def test_under_threshold_no_op(memory_dir, handoffs_dir, capsys):
    write_memory_index(memory_dir, [f"- entry {i}" for i in range(5)])
    parent_before = {path.name for path in memory_dir.parent.iterdir()}
    code = run(cfg(memory_dir, handoffs_dir, max_lines=180), apply=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "no action" in out.lower() or "under threshold" in out.lower()
    assert {path.name for path in memory_dir.parent.iterdir()} == parent_before


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


def test_apply_refuses_flatten_marker_without_preserved_payload(
    memory_dir, handoffs_dir
):
    from memory_doctor import compact as compact_mod

    card = write_card(memory_dir, "topic-marker-only", "original body\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [topic-marker-only](topic-marker-only.md) - first line",
            "  detail that must remain represented",
        ],
    )
    plan = plan_compaction(memory_dir, max_lines=1)
    marker = compact_mod._flatten_marker(
        compact_mod.dt.date.today().isoformat(),
        plan.flattens[0],
    )
    card.write_text(f"original body\n\n{marker}\n")
    original_card = card.read_bytes()
    original_index = index.read_bytes()

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert card.read_bytes() == original_card
    assert index.read_bytes() == original_index


def test_apply_refuses_tighten_marker_without_preserved_payload(
    memory_dir, handoffs_dir
):
    from memory_doctor import compact as compact_mod

    full_hook = "long hook " * 20
    card = write_card(memory_dir, "topic-tighten-marker", "original body\n")
    index = write_memory_index(
        memory_dir,
        [f"- [topic-tighten-marker](topic-tighten-marker.md) {full_hook}"],
    )
    plan = plan_compaction(memory_dir, max_lines=10, max_hook_chars=40)
    marker = compact_mod._tighten_marker(
        compact_mod.dt.date.today().isoformat(),
        plan.tightens[0],
    )
    card.write_text(f"original body\n\n{marker}\n")
    original_card = card.read_bytes()
    original_index = index.read_bytes()

    assert run(
        cfg(memory_dir, handoffs_dir, max_lines=10, max_hook_chars=40),
        apply=True,
    ) == 2
    assert card.read_bytes() == original_card
    assert index.read_bytes() == original_index


def test_apply_recognizes_complete_legacy_flatten_block(
    memory_dir, handoffs_dir
):
    from memory_doctor import compact as compact_mod

    card = write_card(memory_dir, "topic-legacy-flatten", "original body\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [topic-legacy-flatten](topic-legacy-flatten.md) first line",
            "  legacy detail",
        ],
    )
    plan = plan_compaction(memory_dir, max_lines=1)
    today = compact_mod.dt.date.today().isoformat()
    legacy_marker = compact_mod._legacy_flatten_marker(today, plan.flattens[0])
    legacy_block = compact_mod._flatten_preserved_block(
        today,
        plan.flattens[0],
        marker=legacy_marker,
    )
    card.write_text(f"original body\n\n{legacy_block}")

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 0
    assert card.read_text().count("<!-- compact:") == 1
    assert "legacy detail" not in index.read_text()


def test_apply_recognizes_complete_legacy_tighten_block(
    memory_dir, handoffs_dir
):
    from memory_doctor import compact as compact_mod

    full_hook = "legacy long hook " * 12
    card = write_card(memory_dir, "topic-legacy-tighten", "original body\n")
    index = write_memory_index(
        memory_dir,
        [f"- [topic-legacy-tighten](topic-legacy-tighten.md) {full_hook}"],
    )
    plan = plan_compaction(memory_dir, max_lines=10, max_hook_chars=40)
    today = compact_mod.dt.date.today().isoformat()
    legacy_marker = compact_mod._legacy_tighten_marker(today, plan.tightens[0])
    legacy_block = compact_mod._tighten_preserved_block(
        today,
        plan.tightens[0],
        marker=legacy_marker,
    )
    card.write_text(f"original body\n\n{legacy_block}")

    assert run(
        cfg(memory_dir, handoffs_dir, max_lines=10, max_hook_chars=40),
        apply=True,
    ) == 0
    assert card.read_text().count("<!-- compact:tighten:") == 1
    assert full_hook.strip() not in index.read_text()


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


def test_delimiter_ambiguous_flatten_payloads_get_distinct_markers(
    memory_dir, handoffs_dir
):
    write_card(memory_dir, "topic-delimiter", "original\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [same](topic-delimiter.md) hook|detail",
            "  next",
            "- [same](topic-delimiter.md) hook",
            "  detail|next",
        ],
    )

    assert run(cfg(memory_dir, handoffs_dir, max_lines=2), apply=True) == 0

    topic = (memory_dir / "topic-delimiter.md").read_text()
    assert topic.count("<!-- compact:") == 2
    assert "hook|detail\n\nnext" in topic
    assert "hook\n\ndetail|next" in topic
    assert "\n  next" not in index.read_text()
    assert "\n  detail|next" not in index.read_text()


def test_delimiter_ambiguous_tighten_payloads_get_distinct_markers(
    memory_dir, handoffs_dir
):
    suffix = "long suffix " * 12
    write_card(memory_dir, "topic-tighten-delimiter", "original\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [same](topic-tighten-delimiter.md) hook|" + suffix,
            "- [same|hook](topic-tighten-delimiter.md) " + suffix,
        ],
    )

    assert run(
        cfg(memory_dir, handoffs_dir, max_lines=10, max_hook_chars=40),
        apply=True,
    ) == 0

    topic = (memory_dir / "topic-tighten-delimiter.md").read_text()
    assert topic.count("<!-- compact:tighten:") == 2
    assert "hook|" + suffix.strip() in topic
    assert suffix.strip() in topic
    assert len(index.read_text().splitlines()) == 2


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


# --- non-UTF-8 refusal ---

def test_compact_refuses_non_utf8_index(memory_dir, handoffs_dir, capsys):
    index = memory_dir / "MEMORY.md"
    index.write_bytes(b"# Memory Index\n- entry \xff\xfe broken\n")
    original = index.read_bytes()
    code = run(cfg(memory_dir, handoffs_dir), apply=True)
    assert code == 2
    assert index.read_bytes() == original
    assert "not valid UTF-8" in capsys.readouterr().err


def test_compact_refuses_non_utf8_target_card(memory_dir, handoffs_dir, capsys):
    card = memory_dir / "topic-b.md"
    card.write_bytes(b"# Topic B\n\xff\xfe\n")
    write_memory_index(memory_dir, [
        "- [topic-b](topic-b.md) - first line",
        "  detail line to flatten",
    ])
    original_card = card.read_bytes()
    original_index = (memory_dir / "MEMORY.md").read_bytes()
    code = run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True)
    assert code == 2
    assert card.read_bytes() == original_card
    assert (memory_dir / "MEMORY.md").read_bytes() == original_index
    assert "not valid UTF-8" in capsys.readouterr().err


# --- link-target preservation ---

def test_compact_preserves_link_targets_on_normalize(memory_dir, handoffs_dir):
    # Em dash in the link TARGET must survive apply; title and hook text
    # (the visible parts) are normalized.
    write_card(memory_dir, "topic—x", "body")
    write_memory_index(memory_dir, [
        "- [topic—x](topic—x.md) hook with em — dash",
    ])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=180), apply=True)
    assert code == 0
    text = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "(topic—x.md)" in text
    assert "em - dash" in text
    assert text.startswith("- [topic-x](topic—x.md)")


def test_compact_noop_when_unicode_only_in_link_target(memory_dir, handoffs_dir, capsys):
    write_card(memory_dir, "topic—x", "body")
    write_memory_index(memory_dir, ["- [topic-x](topic—x.md) clean hook"])
    code = run(cfg(memory_dir, handoffs_dir, max_lines=180), apply=True)
    assert code == 0
    assert "no action needed" in capsys.readouterr().out


# --- dirty-tree protection on plain --apply ---

import subprocess


def _commit_all(memory_dir):
    subprocess.run(["git", "-C", str(memory_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(memory_dir), "commit", "--quiet", "-m", "setup"],
        check=True,
    )


def _parent_snapshot(memory_dir: Path) -> set[tuple[str, str]]:
    return {
        (path.name, "dir" if path.is_dir() else "file")
        for path in memory_dir.parent.iterdir()
    }


def test_plain_apply_refuses_dirty_index(git_memory_dir, handoffs_dir, capsys):
    memory_dir = git_memory_dir
    write_card(memory_dir, "topic-b", "## existing\nbody")
    write_memory_index(memory_dir, [
        "- [topic-b](topic-b.md) - first",
        "  detail to flatten",
    ])
    _commit_all(memory_dir)
    with (memory_dir / "MEMORY.md").open("a") as f:
        f.write("- uncommitted local edit\n")
    original = (memory_dir / "MEMORY.md").read_text()
    code = run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True)
    assert code == 2
    assert (memory_dir / "MEMORY.md").read_text() == original
    err = capsys.readouterr().err
    assert "refusing to apply" in err
    assert "MEMORY.md" in err


def test_repeated_cards_prefix_cannot_bypass_dirty_tighten_target(
    git_memory_dir, handoffs_dir
):
    card = write_card(git_memory_dir, "dirty", "original\n")
    index = write_memory_index(
        git_memory_dir,
        [
            "- [dirty](cards/cards/dirty.md) "
            + ("long hook content " * 12).strip()
        ],
    )
    _commit_all(git_memory_dir)
    card.write_text("original\noperator edit\n", encoding="utf-8")
    before_parent = _parent_snapshot(git_memory_dir)
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    assert run(
        cfg(git_memory_dir, handoffs_dir, max_hook_chars=10), apply=True
    ) == 0

    assert _parent_snapshot(git_memory_dir) == before_parent
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index


def test_plain_apply_proceeds_on_clean_tree(git_memory_dir, handoffs_dir):
    memory_dir = git_memory_dir
    write_card(memory_dir, "topic-b", "## existing\nbody")
    write_memory_index(memory_dir, [
        "- [topic-b](topic-b.md) - first",
        "  detail to flatten",
    ])
    _commit_all(memory_dir)
    code = run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True)
    assert code == 0
    assert "detail to flatten" in (memory_dir / "topic-b.md").read_text()


def test_compact_double_apply_reaches_fixed_point(memory_dir, handoffs_dir, capsys):
    # Second apply after a normalization pass must be a no-op with identical bytes.
    write_card(memory_dir, "topic-b", "body")
    write_memory_index(memory_dir, ["- [topic-b](topic-b.md) hook with em — dash"])
    code1 = run(cfg(memory_dir, handoffs_dir, max_lines=180), apply=True)
    assert code1 == 0
    after_first = (memory_dir / "MEMORY.md").read_bytes()
    capsys.readouterr()
    code2 = run(cfg(memory_dir, handoffs_dir, max_lines=180), apply=True)
    assert code2 == 0
    assert "no action needed" in capsys.readouterr().out
    assert (memory_dir / "MEMORY.md").read_bytes() == after_first


def test_compact_restart_recovers_rewritten_index_before_no_work_return(
    memory_dir, handoffs_dir, capsys
):
    from memory_doctor.compact import _apply_flatten
    from memory_doctor.transaction import ApplyTransaction

    card = write_card(memory_dir, "topic-b", "body\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [topic-b](topic-b.md) - first",
            "  detail moved after restart",
        ],
    )
    original_card = card.read_text()
    original_index = index.read_text()
    config = cfg(memory_dir, handoffs_dir, max_lines=1)
    plan = plan_compaction(
        memory_dir,
        config.max_lines,
        max_hook_chars=config.max_hook_chars,
    )
    abandoned = ApplyTransaction(memory_dir)
    abandoned.__enter__()
    abandoned.preflight_mutations()
    _apply_flatten(memory_dir, plan, abandoned)
    recovery_artifacts = [
        record.quarantine
        for record in abandoned._files.values()
        if record.quarantine.exists()
    ]
    journal = abandoned._journal_path
    expected_card = card.read_bytes()
    expected_index = index.read_bytes()
    abandoned._release_lock()

    assert index.read_text() != original_index
    assert card.read_text() != original_card
    assert journal.exists()

    assert run(config, apply=True) == 0

    assert "recovered an interrupted apply transaction" in capsys.readouterr().err
    assert card.read_bytes() == expected_card
    assert index.read_bytes() == expected_index
    assert card.read_text().count("detail moved after restart") == 1
    assert all(not path.exists() for path in recovery_artifacts)
    assert not journal.exists()


def test_compact_rechecks_recovery_after_concurrent_index_rewrite(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import compact as compact_mod
    from memory_doctor.transaction import ApplyTransaction

    card = write_card(memory_dir, "topic-race", "body\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [topic-race](topic-race.md) - first",
            "  detail raced after probe",
        ],
    )
    config = cfg(memory_dir, handoffs_dir, max_lines=1)
    real_probe = compact_mod.has_pending_transaction_recovery
    raced = False

    def crash_after_first_probe(path: Path) -> bool:
        nonlocal raced
        result = real_probe(path)
        if not raced:
            raced = True
            plan = plan_compaction(
                memory_dir,
                config.max_lines,
                max_hook_chars=config.max_hook_chars,
            )
            abandoned = ApplyTransaction(memory_dir)
            abandoned.__enter__()
            abandoned.preflight_mutations()
            compact_mod._apply_flatten(memory_dir, plan, abandoned)
            abandoned._release_lock()
        return result

    monkeypatch.setattr(
        compact_mod, "has_pending_transaction_recovery", crash_after_first_probe
    )

    assert run(config, apply=True) == 0
    assert card.read_text().count("detail raced after probe") == 1
    assert "detail raced after probe" not in index.read_text()


def test_compact_preserves_index_replaced_after_locked_plan(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import compact as compact_mod

    card = write_card(memory_dir, "topic-race", "original card\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [topic-race](topic-race.md) - first",
            "  stale detail",
        ],
    )
    operator_bytes = b"# operator replacement\n"
    real_plan = compact_mod.plan_compaction
    plan_count = 0

    def replace_after_locked_plan(*args, **kwargs):
        nonlocal plan_count
        plan = real_plan(*args, **kwargs)
        plan_count += 1
        if plan_count == 2:
            operator_path = memory_dir / "operator.tmp"
            operator_path.write_bytes(operator_bytes)
            operator_path.replace(index)
        return plan

    monkeypatch.setattr(compact_mod, "plan_compaction", replace_after_locked_plan)

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert index.read_bytes() == operator_bytes
    assert card.read_text() == "original card\n"


def test_compact_preserves_card_replaced_after_locked_plan(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import compact as compact_mod

    card = write_card(memory_dir, "topic-race", "original card\n")
    index = write_memory_index(
        memory_dir,
        [
            "- [topic-race](topic-race.md) - first",
            "  stale detail",
        ],
    )
    original_index = index.read_bytes()
    real_plan = compact_mod.plan_compaction
    plan_count = 0

    def replace_card_after_locked_plan(*args, **kwargs):
        nonlocal plan_count
        plan = real_plan(*args, **kwargs)
        plan_count += 1
        if plan_count == 2:
            operator_path = memory_dir / "operator-card.tmp"
            operator_path.write_text("operator replacement\n")
            operator_path.replace(card)
        return plan

    monkeypatch.setattr(
        compact_mod,
        "plan_compaction",
        replace_card_after_locked_plan,
    )

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert card.read_text() == "operator replacement\n"
    assert index.read_bytes() == original_index


def test_compact_preserves_marker_card_replaced_before_index_rewrite(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import compact as compact_mod

    card = write_card(memory_dir, "topic-marker-race", "original card\n")
    index_lines = [
        "- [topic-marker-race](topic-marker-race.md) - first",
        "  stale detail",
    ]
    index = write_memory_index(memory_dir, index_lines)

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 0
    assert "<!-- compact:" in card.read_text()

    write_memory_index(memory_dir, index_lines)
    original_index = index.read_bytes()
    real_marker = compact_mod._flatten_marker
    replaced = False

    def replace_card_before_marker_check(today, flatten):
        nonlocal replaced
        marker = real_marker(today, flatten)
        if not replaced:
            replaced = True
            operator_path = memory_dir / "operator-marker-card.tmp"
            operator_path.write_text("operator replacement\n")
            operator_path.replace(card)
        return marker

    monkeypatch.setattr(
        compact_mod,
        "_flatten_marker",
        replace_card_before_marker_check,
    )

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert card.read_text() == "operator replacement\n"
    assert index.read_bytes() == original_index


def test_compact_preserves_marker_card_replaced_during_index_write(
    memory_dir, handoffs_dir, monkeypatch
):
    from memory_doctor import transaction as transaction_mod

    card = write_card(memory_dir, "topic-marker-commit", "original card\n")
    index_lines = [
        "- [topic-marker-commit](topic-marker-commit.md) - first",
        "  stale detail",
    ]
    index = write_memory_index(memory_dir, index_lines)

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 0
    assert "<!-- compact:" in card.read_text()

    write_memory_index(memory_dir, index_lines)
    original_index = index.read_bytes()
    real_write = transaction_mod.ApplyTransaction.write_text
    replaced = False

    def replace_card_during_index_write(self, path, content, **kwargs):
        nonlocal replaced
        if path == index and not replaced:
            replaced = True
            operator_path = memory_dir / "operator-marker-commit.tmp"
            operator_path.write_text("operator replacement\n")
            operator_path.replace(card)
        return real_write(self, path, content, **kwargs)

    monkeypatch.setattr(
        transaction_mod.ApplyTransaction,
        "write_text",
        replace_card_during_index_write,
    )

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert card.read_text() == "operator replacement\n"
    assert index.read_bytes() == original_index


def test_compact_rolls_back_all_files_when_late_write_fails(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor import transaction as transaction_mod

    card = write_card(memory_dir, "topic-b", "body\n")
    index = write_memory_index(memory_dir, [
        "- [topic-b](topic-b.md) - first",
        "  detail to flatten",
    ])
    original_card = card.read_text()
    original_index = index.read_text()
    real_write = transaction_mod.atomic_write_text

    def fail_index(path, content, **kwargs):
        if path == index:
            raise OSError("index write exploded")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(transaction_mod, "atomic_write_text", fail_index)

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 1
    assert card.read_text() == original_card
    assert index.read_text() == original_index
    assert "rolled back" in capsys.readouterr().err


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_compact_transaction_construction_failure_is_handled(
    memory_dir, handoffs_dir, monkeypatch, capsys, error_type
):
    card = write_card(memory_dir, "constructor", "body\n")
    index = write_memory_index(
        memory_dir,
        ["- [constructor](constructor.md) hook", "  detail"],
    )
    before_parent = _parent_snapshot(memory_dir)
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    def fail_construction(*args, **kwargs):
        raise error_type("cannot resolve memory root")

    monkeypatch.setattr("memory_doctor.compact.ApplyTransaction", fail_construction)

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    err = capsys.readouterr().err
    assert "transaction recovery incomplete" in err
    assert "cannot resolve memory root" in err
    assert "Traceback" not in err
    assert _parent_snapshot(memory_dir) == before_parent
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_compact_transaction_entry_failure_is_handled(
    memory_dir, handoffs_dir, monkeypatch, capsys, error_type
):
    card = write_card(memory_dir, "entry", "body\n")
    index = write_memory_index(
        memory_dir,
        ["- [entry](entry.md) hook", "  detail"],
    )
    before_parent = _parent_snapshot(memory_dir)
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    def fail_entry(transaction):
        raise error_type("cannot create transaction lock")

    monkeypatch.setattr(
        "memory_doctor.transaction.ApplyTransaction.__enter__", fail_entry
    )

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    err = capsys.readouterr().err
    assert "transaction recovery incomplete" in err
    assert "cannot create transaction lock" in err
    assert "Traceback" not in err
    assert _parent_snapshot(memory_dir) == before_parent
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index


def test_compact_invalid_author_preflight_creates_no_transaction_state(
    git_memory_dir, handoffs_dir
):
    card = write_card(git_memory_dir, "invalid-author", "body\n")
    index = write_memory_index(
        git_memory_dir,
        ["- [invalid-author](invalid-author.md) hook", "  detail"],
    )
    _commit_all(git_memory_dir)
    before_parent = _parent_snapshot(git_memory_dir)
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    assert run(
        cfg(git_memory_dir, handoffs_dir, max_lines=1),
        apply=True,
        commit=True,
        commit_author="bad-author",
    ) == 2

    assert _parent_snapshot(git_memory_dir) == before_parent
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index


def test_compact_non_git_commit_preflight_creates_no_transaction_state(
    memory_dir, handoffs_dir
):
    card = write_card(memory_dir, "non-git", "body\n")
    index = write_memory_index(
        memory_dir,
        ["- [non-git](non-git.md) hook", "  detail"],
    )
    before_parent = _parent_snapshot(memory_dir)
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True, commit=True) == 2

    assert _parent_snapshot(memory_dir) == before_parent
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index


def test_compact_dirty_tree_preflight_creates_no_transaction_state(
    git_memory_dir, handoffs_dir
):
    card = write_card(git_memory_dir, "dirty-no-state", "body\n")
    index = write_memory_index(
        git_memory_dir,
        ["- [dirty-no-state](dirty-no-state.md) hook", "  detail"],
    )
    _commit_all(git_memory_dir)
    index.write_text(index.read_text() + "operator edit\n")
    before_parent = _parent_snapshot(git_memory_dir)
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    assert run(cfg(git_memory_dir, handoffs_dir, max_lines=1), apply=True) == 2

    assert _parent_snapshot(git_memory_dir) == before_parent
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index


def test_compact_revalidates_git_status_under_transaction_lock(
    git_memory_dir, handoffs_dir, monkeypatch
):
    card = write_card(git_memory_dir, "race", "body\n")
    index = write_memory_index(
        git_memory_dir,
        ["- [race](race.md) hook", "  detail"],
    )
    _commit_all(git_memory_dir)
    calls = 0

    def clean_then_dirty(memory_dir, paths):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [(index, " M")]

    monkeypatch.setattr(
        "memory_doctor.git.files_have_uncommitted_changes", clean_then_dirty
    )

    assert run(cfg(git_memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert calls == 2
    assert card.read_text() == "body\n"
    assert "detail" in index.read_text()


def test_compact_runtime_capability_refusal_happens_before_visible_write(
    memory_dir, handoffs_dir, monkeypatch, capsys
):
    from memory_doctor.transaction import TransactionRecoveryError

    card = write_card(memory_dir, "unsupported", "body\n")
    index = write_memory_index(
        memory_dir,
        ["- [unsupported](unsupported.md) hook", "  detail"],
    )
    before_card = card.read_bytes()
    before_index = index.read_bytes()

    def unsupported_filesystem(transaction):
        raise TransactionRecoveryError("filesystem lacks hard-link support")

    monkeypatch.setattr(
        "memory_doctor.transaction.ApplyTransaction.preflight_mutations",
        unsupported_filesystem,
    )

    assert run(cfg(memory_dir, handoffs_dir, max_lines=1), apply=True) == 2
    assert "filesystem lacks hard-link support" in capsys.readouterr().err
    assert card.read_bytes() == before_card
    assert index.read_bytes() == before_index
