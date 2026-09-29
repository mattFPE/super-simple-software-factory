"""Run a committing ADW in its own git worktree, on its own branch.

A commit phase stages with `git add -A`, so an ADW that commits in your
checkout either sweeps your uncommitted work into the agents' commit or has to
refuse to start. A worktree removes the dilemma: the run gets a clean checkout
of HEAD on branch `sssf/<adw_id>`, your checkout is never its working
directory, and runs can go in parallel. The ending is explicit — leave the
branch, merge it, or open a PR.

Only the CODE moves. The ADW process keeps running in your checkout, so the
config, the prompts, the trace db and the session folder stay in one place;
`run.repo_root` is repointed at the worktree, and everything that touches the
codebase (agents' cwd, git, gates, quality, permissions) goes through it.

This is isolation by working directory, not a sandbox: an agent that `cd`s
into your checkout can still write there. Your checkout is deliberately not
policed — you may be editing it while the run works, and a rollback would
undo your edits, not the agent's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import ci, git_helper
from .data_types import PhaseParams, RunOptions
from .utils import operator_env

SETUP_TIMEOUT_SECONDS = 1800
TAIL_CHARS = 2000


def branch_for(adw_id: str) -> str:
    return f"sssf/{adw_id}"


def path_for(main_root: Path, adw_id: str) -> Path:
    """A sibling of the repo, never inside it: a nested checkout would be picked
    up by your own test runners and type checkers as a second copy of the code."""
    return main_root.parent / f"{main_root.name}.sssf-worktrees" / adw_id


def preflight(opts: RunOptions) -> None:
    """Everything a committing ADW needs before any agent spawns."""
    if opts.in_place:
        git_helper.require_committable(opts.allow_dirty)
        return
    if not git_helper.is_repo():
        raise SystemExit("not a git repository — this ADW ends in a commit. Run `git init` "
                         "and make a first commit before running it.")
    if not git_helper.ref_exists("HEAD"):
        raise SystemExit("this repository has no commits yet — a run's worktree is a checkout "
                         "of HEAD. Make a first commit before running it.")
    if opts.land == "pr":
        if not shutil.which("gh"):
            raise SystemExit("--pr needs the GitHub CLI (`gh`) on PATH")
        if not git_helper._ok("remote", "get-url", "origin"):
            raise SystemExit("--pr needs a remote named `origin` to push the branch to")


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _registered(main_root: Path, path: Path) -> bool:
    listing = git_helper._git("worktree", "list", "--porcelain", repo=main_root)
    return any(_same(line[len("worktree "):], path)
               for line in listing.splitlines() if line.startswith("worktree "))


def _create(run, main_root: Path) -> dict:
    path = path_for(main_root, run.adw_id)
    branch = branch_for(run.adw_id)
    base_branch = git_helper.current_branch(main_root)
    if _registered(main_root, path):
        # A joined run (--adw-id) or a retry after a failure: keep its work.
        run.console.note(f"reusing worktree {path}")
        # Copy and setup again: a reuse usually follows a failure, often a
        # failed setup, and both are meant to be safe to repeat.
        _copy(run, main_root, path)
        _setup(run, path)
        return {"path": path, "branch": branch, "base_branch": base_branch,
                # where the branch left base_branch — not today's HEAD, which may
                # have moved on while the failed run sat waiting
                "base_sha": git_helper.merge_base(base_branch, "HEAD", path), "reused": True}
    git_helper._git("worktree", "prune", repo=main_root)   # forget dirs deleted by hand
    if path.exists():
        raise RuntimeError(f"{path} exists but is not a git worktree — move it aside")
    path.parent.mkdir(parents=True, exist_ok=True)
    if git_helper.branch_exists(branch, main_root):
        git_helper._git("worktree", "add", str(path), branch, repo=main_root)
    else:
        git_helper._git("worktree", "add", "-b", branch, str(path), "HEAD", repo=main_root)
    info = {"path": path, "branch": branch, "base_branch": base_branch,
            "base_sha": git_helper.rev("HEAD", main_root), "reused": False}
    # Said before copy/setup, which are the steps most likely to fail.
    run.console.note(f"worktree {path} on {branch} — kept if the run fails "
                     f"(rerun with --adw-id {run.adw_id}, or `just worktree-rm {run.adw_id}`)")
    _copy(run, main_root, path)
    _setup(run, path)
    return info


def _copy(run, main_root: Path, path: Path) -> None:
    for rel in run.cfg.worktree.copy_files:
        src, dest = main_root / rel, path / rel
        if not src.exists():
            run.console.note(f"worktree copy: {rel} not found in your checkout — skipped")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        run.console.note(f"worktree copy: {rel}")


def _setup(run, path: Path) -> None:
    from .quality import resolve_argv            # one resolver: PATHEXT, operator PATH
    log = run.session_dir / "worktree_setup.log"
    for argv in run.cfg.worktree.setup:
        command = " ".join(argv)
        run.console.note(f"worktree setup: {command}")
        try:
            done = subprocess.run(resolve_argv(argv), cwd=path, env=operator_env(),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=SETUP_TIMEOUT_SECONDS)
            code, output = done.returncode, done.stdout + done.stderr
        except (OSError, subprocess.TimeoutExpired) as error:
            code, output = 127, str(error)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"$ {command}\nexit: {code}\n{output}\n")
        if code != 0:
            raise RuntimeError(f"worktree setup `{command}` exited {code} (log: {log})\n"
                               f"{output[-TAIL_CHARS:]}")


def _pr_body(run, opts: RunOptions, ahead: int) -> str:
    """What the PR says: the issue it closes, what the run checked, who opened it."""
    parts = []
    if opts.issue:
        # Same repo by construction (issues.load refuses any other), so the bare
        # form links, and GitHub closes the issue when the PR merges.
        refs = f"Closes #{opts.issue.number}"
        if opts.issue.parent:
            refs += f"\nPart of #{opts.issue.parent.number}"
        parts.append(refs)
    parts += run.report.values()          # what the run checked: tests, review
    parts.append(f"Opened by SSSF run `{run.adw_id}` ({ahead} commit(s)).")
    return "\n\n".join(parts)


def _land(run, opts: RunOptions) -> dict:
    mode = opts.land
    wt = run.worktree
    main_root = run.main_root
    head = git_helper.short_sha("HEAD", wt["path"])
    ahead = int(git_helper._git("rev-list", "--count", f"{wt['base_sha']}..HEAD",
                                repo=wt["path"]))
    outcome = {"branch": wt["branch"], "head": head, "commits": ahead, "land": mode}
    if ahead == 0:
        outcome["note"] = "no commits on the branch — nothing to land"
    elif mode == "merge":
        current = git_helper.current_branch(main_root)
        if current != wt["base_branch"]:
            raise RuntimeError(
                f"your checkout is on {current!r}, but the run started from "
                f"{wt['base_branch']!r} — merge by hand: git merge {wt['branch']}")
        # Refuses on its own if your uncommitted edits touch the same files;
        # the branch is untouched either way.
        git_helper._git("merge", "--no-edit", wt["branch"], repo=main_root)
        outcome["merged_into"] = wt["base_branch"]   # branch deleted once the worktree is gone
    elif mode == "pr":
        # An issue names the work better than the last commit does, which in a
        # chain that commits per product is the write-up, not the change.
        subject = (opts.issue.title if opts.issue
                   else git_helper._git("log", "-1", "--format=%s", repo=wt["path"]))
        git_helper._git("push", "-u", "origin", wt["branch"], repo=wt["path"])
        target = git_helper.origin_repo(wt["path"])
        done = subprocess.run(
            ["gh", "pr", "create", "--repo", target,
             "--base", wt["base_branch"], "--head", wt["branch"],
             "--title", subject,
             "--body", _pr_body(run, opts, ahead)],
            cwd=wt["path"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=operator_env())
        if done.returncode != 0:
            raise RuntimeError(f"gh pr create failed: {done.stderr.strip()[-TAIL_CHARS:]} "
                               f"(the branch {wt['branch']} is pushed)")
        outcome["pr"] = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        outcome["pr_repo"] = target
    else:
        outcome["merge_with"] = f"git merge {wt['branch']}"
    return outcome


def _remove(run) -> None:
    """The branch holds the work; the directory is only scaffolding."""
    try:
        git_helper._git("worktree", "remove", "--force", str(run.worktree["path"]),
                        repo=run.main_root)
    except RuntimeError as error:
        run.console.note(f"worktree left at {run.worktree['path']}: {error}")


def enter(run, opts: RunOptions) -> None:
    """The `worktree` phase: check out HEAD on its own branch and move the run there.

    Call right after the request phase. In place it does nothing (preflight
    already demanded a clean tree). A failed run keeps its worktree so its state
    can be inspected; rerunning with the same --adw-id picks it back up.
    """
    if opts.in_place:
        return
    with run.phase(PhaseParams(
            name="worktree", kind="code", owner="git",
            description="Check out HEAD into its own worktree and branch, so the run "
                        "never touches your checkout")) as ph:
        run.worktree = _create(run, run.main_root)
        run.repo_root = run.worktree["path"]
        if not run.worktree["reused"] and git_helper.is_dirty(run.main_root):
            run.console.note("your uncommitted changes are NOT in this run — it works from "
                             f"your last commit ({run.worktree['base_sha'][:7]})")
        ph.log(path=str(run.worktree["path"]), branch=run.worktree["branch"],
               base=f"{run.worktree['base_branch']} @ {run.worktree['base_sha'][:7]}",
               reused=run.worktree["reused"])


def land(run, opts: RunOptions) -> None:
    """The `land` phase: end the run as a branch, a merge or a PR; drop the worktree.
    A PR is then followed by the `checks` phase when `pr.wait_for_checks` is on.

    Call after the last commit phase. In place it does nothing — the commits are
    already on your branch.
    """
    if opts.in_place:
        return
    with run.phase(PhaseParams(
            name="land", kind="code", owner="git",
            description=f"End the run as a {opts.land}: the work is on its branch, now "
                        "decide where it goes")) as ph:
        outcome = _land(run, opts)
        run.landed = outcome              # the issue's closing comment links the PR
        _remove(run)
        if outcome.get("merged_into"):
            # Only now: git refuses to delete a branch a worktree has checked out.
            git_helper._git("branch", "-d", run.worktree["branch"], repo=run.main_root)
        ph.log(**outcome)
    ci.wait(run)
