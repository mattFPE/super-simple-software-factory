"""Low-level git operations for code phases. All low-level logic lives in adw_modules.

Every function takes `repo`: the checkout to run in. Pass `run.repo_root`, which
is the run's worktree when it has one (see worktree.py) and the engineer's
checkout otherwise. Omitted, git runs in the process cwd.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

Repo = Path | str | None


def _git(*args: str, repo: Repo = None) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=repo,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _ok(*args: str, repo: Repo = None) -> bool:
    """Run git as a yes/no question. Never raises."""
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=repo).returncode == 0


def current_branch(repo: Repo = None) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", repo=repo)


def create_branch(name: str, repo: Repo = None) -> str:
    _git("checkout", "-b", name, repo=repo)
    return name


def branch_exists(name: str, repo: Repo = None) -> bool:
    return _ok("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", repo=repo)


def is_repo(repo: Repo = None) -> bool:
    return _ok("rev-parse", "--git-dir", repo=repo)


def repo_root(repo: Repo = None) -> Path:
    """Absolute root of the codebase — where agents are spawned to work.

    The git toplevel when there is one, else the process cwd (ADWs run fine in a
    non-git dir; only a commit phase requires a repo). Always absolute, so it is
    safe to hand to a subprocess regardless of where the ADW was launched from.
    """
    if is_repo(repo):
        return Path(_git("rev-parse", "--show-toplevel", repo=repo)).resolve()
    return Path(repo or Path.cwd()).resolve()


def require_committable(allow_dirty: bool = False, repo: Repo = None) -> None:
    """Preflight for an ADW that commits IN PLACE. Call before any agent spawns.

    `commit_all` stages with `git add -A`, so anything already uncommitted when
    the run starts would be committed as the agents' work — the engineer's own
    edits included. Refusing a dirty tree up front costs nothing; finding out
    from the commit costs the whole run, or worse, succeeds. Checking for a repo
    here also moves that failure from the last phase to before the first.
    (A run in its own worktree starts from a clean checkout and needs none of this.)
    """
    if not is_repo(repo):
        raise SystemExit(
            "not a git repository — this ADW ends in a commit. Run `git init` and make "
            "a first commit before running it.")
    if allow_dirty:
        return
    dirty = _git("status", "--porcelain", repo=repo).splitlines()
    if dirty:
        listing = "\n".join(f"  {line}" for line in dirty[:20])
        more = f"\n  … and {len(dirty) - 20} more" if len(dirty) > 20 else ""
        raise SystemExit(
            f"working tree has {len(dirty)} uncommitted change(s) that this ADW's commit "
            f"would sweep in as the agents' work:\n{listing}{more}\n"
            "Commit or stash them first, pass --allow-dirty to include them on purpose, "
            "or drop --in-place to run in a worktree of your last commit.")


def commit_all(message: str, repo: Repo = None) -> str:
    """Stage the working tree and commit it. Returns the new short sha."""
    if not is_repo(repo):
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the "
            "repo root (and make a first commit) before running an ADW that commits.")
    _git("add", "-A", repo=repo)
    if not _git("status", "--porcelain", repo=repo):
        raise RuntimeError("nothing to commit — the preceding phases changed no files")
    _git("commit", "-m", message, repo=repo)
    return _git("rev-parse", "--short", "HEAD", repo=repo)


def changed_files(repo: Repo = None) -> list[str]:
    out = _git("status", "--porcelain", repo=repo)
    return [line[3:] for line in out.splitlines() if line]


# ── diff plumbing (composed into a ChangeSet by documentation.py) ────────────

def ref_exists(ref: str, repo: Repo = None) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    return _ok("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", repo=repo)


def rev(ref: str = "HEAD", repo: Repo = None) -> str:
    return _git("rev-parse", ref, repo=repo)


def short_sha(ref: str = "HEAD", repo: Repo = None) -> str:
    return _git("rev-parse", "--short", ref, repo=repo)


def merge_base(ref: str, other: str = "HEAD", repo: Repo = None) -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no ADW has to branch on it.
    """
    return _git("merge-base", ref, other, repo=repo)


def is_dirty(repo: Repo = None) -> bool:
    return bool(_git("status", "--porcelain", repo=repo))


def untracked_files(repo: Repo = None) -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard", repo=repo)
    return [line for line in out.splitlines() if line]


def diff_files(base: str, repo: Repo = None) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git("diff", "--name-only", base, repo=repo)
    return [line for line in out.splitlines() if line]


def diff_stat(base: str, repo: Repo = None) -> str:
    return _git("diff", "--stat", base, repo=repo)


def diff_counts(base: str, repo: Repo = None) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git("diff", "--numstat", base, repo=repo).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(base: str, repo: Repo = None) -> str:
    return _git("diff", base, repo=repo)
