"""init-git verb: bootstrap a memory dir as a git repo with one initial commit."""
from __future__ import annotations

import shlex
import subprocess
import stat
import sys
from pathlib import Path

from memory_doctor.git import is_git_repo
from memory_doctor.paths import PathConfig
from memory_doctor.safety import atomic_write_text


def _run_git(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run Git with captured diagnostics, returning None when it cannot start."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        print(f"memory-doctor init-git: could not run git: {exc}", file=sys.stderr)
        return None


def _git_identity_is_configured(memory_dir: Path) -> bool:
    """Validate the effective Git author before creating or staging files."""
    missing: list[str] = []
    for key in ("user.name", "user.email"):
        result = _run_git(["git", "-C", str(memory_dir), "config", "--get", key])
        if result is None:
            return False
        if result.returncode == 1 or (
            result.returncode == 0 and not result.stdout.strip()
        ):
            missing.append(key)
        elif result.returncode != 0:
            _report_step_failure(f"git config {key}", result)
            return False
    if not missing:
        return True
    print(
        "memory-doctor init-git: Git identity is not configured: "
        + ", ".join(missing)
        + "\n  fix: configure user.name and user.email, then rerun `memory-doctor init-git`",
        file=sys.stderr,
    )
    return False


def _initial_pathspecs(memory_dir: Path) -> list[str]:
    """Return the top-level memory files owned by the initial import."""
    paths = [".gitignore"]
    index = memory_dir / "MEMORY.md"
    if index.exists():
        paths.append(index.name)
    paths.extend(
        p.name for p in sorted(memory_dir.glob("*.md")) if p.name != "MEMORY.md"
    )
    return paths


def _initial_paths_are_regular_files(memory_dir: Path) -> bool:
    """Reject import paths that Git would recursively expand or misinterpret."""
    try:
        candidates = [memory_dir / ".gitignore", memory_dir / "MEMORY.md"]
        candidates.extend(
            p for p in sorted(memory_dir.glob("*.md")) if p.name != "MEMORY.md"
        )
    except OSError as exc:
        print(
            f"memory-doctor init-git: could not inspect initial import paths: {exc}",
            file=sys.stderr,
        )
        return False

    for path in candidates:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(
                f"memory-doctor init-git: could not inspect initial import path {path}: {exc}",
                file=sys.stderr,
            )
            return False
        if not stat.S_ISREG(mode):
            print(
                "memory-doctor init-git: initial import path is not a regular file: "
                f"{path}\n"
                "  recovery: move or replace that path, then rerun `memory-doctor init-git`",
                file=sys.stderr,
            )
            return False
    return True


def _report_step_failure(step: str, result: subprocess.CompletedProcess[str]) -> int:
    detail = result.stderr.strip() or result.stdout.strip()
    print(
        f"memory-doctor init-git: {step} failed: {detail or f'exit {result.returncode}'}\n"
        "  recovery: fix the reported Git error, then rerun `memory-doctor init-git`",
        file=sys.stderr,
    )
    return 2


def _report_post_commit_verification_failure(
    memory_dir: Path, result: subprocess.CompletedProcess[str] | None
) -> int:
    """Report recovery that remains usable after the initial commit exists."""
    if result is None:
        detail = "Git command could not start; see the prior diagnostic"
    else:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    verify_command = shlex.join(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"]
    )
    status_command = shlex.join(["git", "-C", str(memory_dir), "status"])
    print(
        "memory-doctor init-git: initial commit may have succeeded, but final HEAD "
        f"verification failed: {detail}\n"
        f"  recovery: inspect the existing repository with `{verify_command}` and "
        f"`{status_command}`; resolve any reported Git error manually",
        file=sys.stderr,
    )
    return 2


def run(cfg: PathConfig) -> int:
    memory_dir = cfg.memory_dir
    if not memory_dir.exists():
        print(f"memory-doctor init-git: memory dir does not exist: {memory_dir}", file=sys.stderr)
        return 2
    repo_exists = is_git_repo(memory_dir)
    if repo_exists:
        head = _run_git(
            ["git", "-C", str(memory_dir), "rev-parse", "--verify", "--quiet", "HEAD"]
        )
        if head is None:
            return 2
        if head.returncode == 0:
            print(
                f"memory-doctor init-git: memory dir is already a git repo: {memory_dir}",
                file=sys.stderr,
            )
            return 2
        if head.returncode != 1:
            return _report_step_failure("git rev-parse HEAD", head)
    elif (memory_dir / ".git").exists():
        print(
            f"memory-doctor init-git: found an invalid partial repository at {memory_dir / '.git'}\n"
            "  recovery: inspect or remove that .git entry after backing it up, then rerun",
            file=sys.stderr,
        )
        return 2

    if not repo_exists:
        # Initialize with `main` as the default branch for predictability across
        # git versions; older defaults of `master` are inconsistent.
        initialized = _run_git(
            ["git", "init", "--quiet", "-b", "main", str(memory_dir)]
        )
        if initialized is None:
            return 2
        if initialized.returncode != 0:
            return _report_step_failure("git init", initialized)

    if not _git_identity_is_configured(memory_dir):
        return 2

    if not _initial_paths_are_regular_files(memory_dir):
        return 2

    # Reserve .gitignore as a customization point even though we don't
    # exclude anything yet. Operators frequently add `.DS_Store` or editor
    # backup patterns; better to have the file present than discover the
    # need later.
    gitignore = memory_dir / ".gitignore"
    if not gitignore.exists():
        atomic_write_text(gitignore, "")

    cards = sorted(p for p in memory_dir.glob("*.md") if p.name != "MEMORY.md")
    has_index = (memory_dir / "MEMORY.md").exists()
    pieces = [f"{len(cards)} cards"]
    if has_index:
        pieces.append("MEMORY.md")
    summary = ", ".join(pieces)
    subject = f"memory: initial import ({summary})"

    pathspecs = _initial_pathspecs(memory_dir)
    added = _run_git(
        ["git", "--literal-pathspecs", "-C", str(memory_dir), "add", "--", *pathspecs]
    )
    if added is None:
        return 2
    if added.returncode != 0:
        return _report_step_failure("git add", added)

    commit = _run_git(
        [
            "git",
            "--literal-pathspecs",
            "-C",
            str(memory_dir),
            "commit",
            "--quiet",
            "-m",
            subject,
            "--",
            *pathspecs,
        ]
    )
    if commit is None:
        return 2
    if commit.returncode != 0:
        print(
            "memory-doctor init-git: initial commit failed: "
            f"{commit.stderr.strip() or commit.stdout.strip() or f'exit {commit.returncode}'}\n"
            "  recovery: fix the reported Git error, then rerun `memory-doctor init-git`",
            file=sys.stderr,
        )
        return 2

    sha_result = _run_git(
        ["git", "-C", str(memory_dir), "rev-parse", "--short=12", "HEAD"],
    )
    if sha_result is None or sha_result.returncode != 0:
        return _report_post_commit_verification_failure(memory_dir, sha_result)
    sha = sha_result.stdout.strip()
    print(f"memory-doctor init-git: initialized {memory_dir} at {sha}")
    return 0
