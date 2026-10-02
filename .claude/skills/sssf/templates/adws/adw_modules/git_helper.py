"""Low-level git operations for code phases. All low-level logic lives in adw_modules.

Every function takes `repo`: the checkout to run in. Pass `run.repo_root`, which
is the run's worktree when it has one (see worktree.py) and the engineer's
checkout otherwise. Omitted, git runs in the process cwd.
"""

from __future__ import annotations

import re
import subprocess
from contextlib import contextmanager
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
                          encoding="utf-8", errors="replace", cwd=repo).returncode == 0


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


def commit_paths(message: str, paths: list[str], repo: Repo = None) -> str | None:
    """Commit only `paths`, leaving the rest of the tree exactly as it is.

    For a phase whose work product is a few named files in a tree that may hold
    someone else's uncommitted work — a joined run's plan commit, with a crashed
    build's changes still in the worktree. Paths outside the repo (a report in
    the session folder) are skipped. Returns the short sha, or None when the
    paths hold no change: a rerun that wrote the same spec has nothing to add.
    """
    if not is_repo(repo):
        raise RuntimeError("not a git repository — a commit phase needs one.")
    root = repo_root(repo)
    inside = []
    for declared in paths:
        path = Path(declared) if Path(declared).is_absolute() else root / declared
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if path.exists():
            inside.append(relative)
    if not inside:
        return None
    _git("add", "-A", "--", *inside, repo=repo)
    if _ok("diff", "--cached", "--quiet", "--", *inside, repo=repo):
        return None
    # A pathspec makes this `--only`: whatever else is staged stays staged, uncommitted.
    _git("commit", "-m", message, "--", *inside, repo=repo)
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


# ── remotes ──────────────────────────────────────────────────────────────────

def origin_url(repo: Repo = None) -> str:
    """The URL of the remote named `origin`. Raises RuntimeError when there is none."""
    return _git("remote", "get-url", "origin", repo=repo)


def origin_repo(repo: Repo = None) -> str:
    """`[HOST/]OWNER/REPO` of the remote a run's branch is pushed to.

    Passed to gh as --repo, never left to gh's default: in a fork with an
    `upstream` remote, gh's default repository is often the PARENT, so a bare
    `gh pr create` would open the PR on someone else's project — and a bare
    `gh issue view 42` would read someone else's issue.
    """
    url = origin_url(repo)
    # https://host/o/r(.git) · ssh://git@host/o/r · git@host:o/r — scheme and user optional
    match = re.match(r"^(?:\w+://)?(?:[^@/]+@)?([^/:]+)[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not match:
        raise RuntimeError(f"cannot tell which repository `origin` is from {url!r}")
    host, owner, name = match.groups()
    return f"{owner}/{name}" if host == "github.com" else f"{host}/{owner}/{name}"


# ── the uncommitted change, as files ─────────────────────────────────────────

def changed_paths(repo: Repo = None) -> list[str]:
    """Every file the working tree changed since HEAD: modified, deleted, new.

    Repo-relative, forward slashes, renames split into a delete and an add —
    one path per file that would have to be put back to undo the change.
    """
    tracked = _git("diff", "--name-only", "--no-renames", "-z", "HEAD", repo=repo)
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z", repo=repo)
    return sorted({p for p in (tracked + "\0" + untracked).split("\0") if p})


def _head_blob(path: str, repo: Repo) -> bytes | None:
    """The file as HEAD has it, byte for byte; None when HEAD has no such file."""
    done = subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, cwd=repo)
    return done.stdout if done.returncode == 0 else None


def _put(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


@contextmanager
def reverted(paths: list[str], repo: Path, backup_dir: Path):
    """Put `paths` back to how HEAD has them for the block, then restore them exactly.

    Plain file writes, not `git stash`: what comes back is the bytes that were
    there, whatever the block did in between, with no merge to conflict. Each
    file is also copied into `backup_dir` first, so if the process is killed
    mid-block the work is still on disk to copy back by hand.
    """
    saved: dict[str, bytes | None] = {}
    for rel in paths:
        path = Path(repo) / rel
        saved[rel] = path.read_bytes() if path.is_file() else None
        if saved[rel] is not None:
            _put(Path(backup_dir) / rel, saved[rel])
    try:
        for rel in paths:
            _put(Path(repo) / rel, _head_blob(rel, repo))
        yield
    finally:
        for rel, content in saved.items():
            _put(Path(repo) / rel, content)
