# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""A GitHub issue as a run's request: read it, decide it may run, claim it, report back.

Specs come from /to-spec and tickets from /to-tickets (mattpocock/skills); both
land as GitHub issues, and an issue is a prompt like any other:

    just sdlc "#42"                     quoted: an unquoted # starts a comment
    just sdlc https://github.com/<owner>/<repo>/issues/42

Whether it may run is already on the tracker, so no flag decides it:

    a ticket                  runs, with its parent spec attached as context
    a spec with no tickets    runs as its own ticket — a small spec isn't split
    a spec with tickets       refuses, and names the tickets to run instead

Only an issue carrying the repo's ready-for-agent label becomes a prompt. The
body is text anyone who can open an issue wrote; the label is the one thing
only a triager can set, so it is the proof someone with rights read it first.
Comments count only from the owner, members and collaborators, for the same
reason — a comment can be added after the label was.

A committing run claims the issue (label `agent-running`) and, when it settles,
removes the label and comments with the outcome. Done needs no label of its
own: the PR says `Closes #42`, so merging it closes the issue. A failed run
leaves the ready label where it was, so a rerun is just a rerun.

Everything goes through `gh api` against origin's repository, never gh's
default, which in a fork is the parent (see git_helper.origin_repo).

This file is also the command line the Console lists a repo's Ready issues
with, each with the verdict a Launch would reach; the Console never asks
GitHub itself:

    uv run adws/adw_modules/issues.py --list-ready [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # A script has no package, so re-enter through adw_modules (see main) —
    # here, before the relative imports below, which would fail without one.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from adw_modules import issues
    sys.exit(issues.main())

from . import git_helper, worktree
from .data_types import Issue, IssueLink, PhaseParams, RunOptions
from .utils import operator_env

RUNNING_LABEL = "agent-running"
READY_ROLE = "ready-for-agent"             # the role's canonical name in mattpocock/skills
TRIAGE_LABELS = Path("docs") / "agents" / "triage-labels.md"   # /setup-matt-pocock-skills
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}                   # whose comments reach an agent
MARKER = "<!-- sssf -->"                   # on our own comments, so they are never fed back in
CHECKLIST_HEADING = "Review checklist"     # what gates.checklist_covered reads back
CHECKLIST_SOURCES = ("Acceptance criteria", "User Stories")   # a ticket's, else a spec's
TAIL_CHARS = 1500
LIST_LIMIT = 50                            # the most Ready issues --list-ready reports
LIST_PAGE = 100                            # GitHub's largest page: the one --list-ready reads

_NUMBER = re.compile(r"^#(\d+)$")
_URL = re.compile(r"^https?://([^/\s]+)/([^/\s]+)/([^/\s]+)/issues/(\d+)/?(?:[?#]\S*)?$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
_CLOSING_PRS = ("query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name)"
                "{issue(number:$n){closedByPullRequestsReferences(first:20,includeClosedPrs:false)"
                "{nodes{url state}}}}}")


# ── is it an issue ───────────────────────────────────────────────────────────

def parse_ref(arg: str) -> tuple[str | None, int] | None:
    """(repo, number) when `arg` names an issue, else None. `#42` names no repo."""
    text = arg.strip()
    if match := _NUMBER.match(text):
        return None, int(match.group(1))
    if match := _URL.match(text):
        host, owner, name, number = match.groups()
        repo = f"{owner}/{name}" if host == "github.com" else f"{host}/{owner}/{name}"
        return repo, int(number)
    return None


# ── read it ──────────────────────────────────────────────────────────────────

@cache
def load(arg: str) -> Issue:
    """The issue `arg` names, read once per process — prompt and options share it."""
    named_repo, number = parse_ref(arg)
    try:
        repo = github_repo()
    except Unavailable as why:
        raise SystemExit(f"an issue as the prompt needs GitHub, but {why}")
    if named_repo and named_repo.casefold() != repo.casefold():
        raise SystemExit(f"{arg} is on {named_repo}, but this checkout's origin is {repo}. "
                         "A run's PR goes to origin, so it can only close origin's issues.")
    try:
        return _read(repo, number)
    except RuntimeError as error:
        raise SystemExit(f"cannot read issue #{number} on {repo}: {error}")


class Unavailable(Exception):
    """Why this checkout cannot reach its GitHub issues, and what to do about it."""

    def __init__(self, reason: str, fix: str):
        super().__init__(f"{reason}. {fix}")
        self.reason, self.fix = reason, fix


def github_repo() -> str:
    """Origin's `[HOST/]OWNER/REPO`, once `gh` can reach it. Raises Unavailable."""
    env = operator_env()
    gh = shutil.which("gh", path=env.get("PATH"))
    if not gh:
        raise Unavailable("the GitHub CLI (`gh`) is not on PATH",
                          "Install it from https://cli.github.com, then run `gh auth login`.")
    try:
        url = git_helper.origin_url()
    except RuntimeError:
        raise Unavailable("this checkout has no remote named `origin`",
                          "Add its GitHub repository: `git remote add origin <url>`.")
    try:
        repo = git_helper.origin_repo()
    except RuntimeError:
        raise Unavailable(f"`origin` ({url}) is not on GitHub",
                          "Point it at the repository on GitHub: `git remote set-url origin <url>`.")
    host = _split(repo)[0] or "github.com"
    status = subprocess.run([gh, "auth", "status", "--hostname", host], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=env)
    if status.returncode == 0:
        return repo
    if host == "github.com":
        raise Unavailable("`gh` is not logged in to github.com", "Run `gh auth login`.")
    # Any other host is GitHub Enterprise only if gh can log in to it.
    raise Unavailable(f"`origin` is on {host}, which is not on GitHub, or not a GitHub "
                      "Enterprise host `gh` is logged in to",
                      f"Point `origin` at GitHub, or run `gh auth login --hostname {host}`.")


def _read(repo: str, number: int) -> Issue:
    path = f"repos/{_slug(repo)}/issues/{number}"
    raw = _api(path, repo)
    if "pull_request" in raw:            # issues and PRs share one number space
        raise RuntimeError(f"#{number} is a pull request, not an issue")
    body = _text(raw.get("body"))
    parent = _parent(repo, number, body)
    checklist, source = _checklist(body)
    return Issue(
        repo=repo, number=number, title=raw["title"], url=raw["html_url"],
        state=raw["state"], body=body, labels=_labels(raw), ready_label=ready_label(),
        parent=_link(parent) if parent else None,
        parent_body=_text(parent.get("body")) if parent else "",
        comments=_comments(repo, number) if raw.get("comments") else [],
        checklist=checklist, checklist_source=source,
        **_verdict_fields(repo, raw, body))


def _verdict_fields(repo: str, raw: dict, body: str) -> dict:
    """What `problems` rules on beyond the issue itself: its tickets, blockers, open PRs."""
    path = f"repos/{_slug(repo)}/issues/{raw['number']}"
    return dict(
        tickets=([_link(item) for item in _api_list(f"{path}/sub_issues", repo)]
                 if (raw.get("sub_issues_summary") or {}).get("total") else []),
        blockers=_blockers(repo, raw, body),
        open_prs=_open_prs(repo, raw["number"]))


def ready_label() -> str:
    """This repo's label for the ready-for-agent role, from the triage-labels.md
    table /setup-matt-pocock-skills writes; the role's own name without one."""
    path = git_helper.repo_root() / TRIAGE_LABELS
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            cells = [cell.strip().strip("`").strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] == READY_ROLE and cells[1]:
                return cells[1]
    return READY_ROLE


def _parent(repo: str, number: int, body: str) -> dict | None:
    """The spec a ticket was cut from: GitHub's sub-issue parent, else the
    ticket's `## Parent` section or a `Part of #N` line."""
    raw = _api_optional(f"repos/{_slug(repo)}/issues/{number}/parent", repo)
    if raw is None:
        text = _section(body, "Parent") + "\n" + "\n".join(
            re.findall(r"(?im)^\W*part of\b(.*)$", body))
        refs = _refs(text, repo)
        raw = _api(f"repos/{_slug(repo)}/issues/{refs[0]}", repo) if refs else None
    return raw


def _blockers(repo: str, raw: dict, body: str) -> list[IssueLink]:
    """Every issue this one is blocked by: GitHub's native dependencies, plus the
    `## Blocked by` section /to-tickets writes where those are not available."""
    found: dict[int, IssueLink] = {}
    if (raw.get("issue_dependencies_summary") or {}).get("total_blocked_by"):
        path = f"repos/{_slug(repo)}/issues/{raw['number']}/dependencies/blocked_by"
        for item in _api_list(path, repo):
            found[item["number"]] = _link(item)
    text = _section(body, "Blocked by") + "\n" + "\n".join(
        re.findall(r"(?im)^\W*blocked by\b(.*)$", body))
    for ref in _refs(text, repo):
        if ref not in found:
            found[ref] = _link(_api(f"repos/{_slug(repo)}/issues/{ref}", repo))
    return list(found.values())


def _comments(repo: str, number: int) -> list[str]:
    rendered = []
    for comment in _api_list(f"repos/{_slug(repo)}/issues/{number}/comments", repo):
        body = _text(comment.get("body")).strip()
        if comment.get("author_association") in TRUSTED and body and MARKER not in body:
            rendered.append(f"**@{comment['user']['login']}** "
                            f"({comment['created_at'][:10]}):\n\n{body}")
    return rendered


def _open_prs(repo: str, number: int) -> list[str]:
    owner, name = _slug(repo).split("/")
    data = _api_optional("graphql", repo, "-f", f"owner={owner}", "-f", f"name={name}",
                         "-F", f"n={number}", "-f", f"query={_CLOSING_PRS}", method="POST")
    try:
        nodes = data["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"]
    except (TypeError, KeyError):
        return []
    return [node["url"] for node in nodes if node.get("state") == "OPEN"]


def _checklist(body: str) -> tuple[list[str], str]:
    """What the review rules on: a ticket's acceptance criteria, else a spec's user stories."""
    for source in CHECKLIST_SOURCES:
        items = _items(_section(body, source))
        if items:
            return items, source
    return [], ""


# ── may it run ───────────────────────────────────────────────────────────────

def require_ready(issue: Issue) -> None:
    """The trust gate, for every ADW: only a triaged issue becomes a prompt."""
    if issue.ready_label not in issue.labels:
        raise SystemExit(
            f"#{issue.number} is not labelled `{issue.ready_label}`, so it does not run. "
            "Its body is text anyone who can open an issue wrote; the label is how a "
            "triager says they read it and it is ready for an agent. Add the label if it is.")


def require_runnable(issue: Issue, force: bool = False) -> None:
    """For a run that ends in a commit: everything that makes it the wrong issue to work now."""
    found = problems(issue, force)
    if found:
        raise SystemExit("\n".join(problem["why"] for problem in found))


def problems(issue: Issue, force: bool = False) -> list[dict]:
    """Why `issue` is not Runnable, in the order require_runnable prints them: each
    a `verdict`, the `why` it says, and the issues or PRs involved. Empty when it is
    Runnable. --list-ready reports the first, so it and a Launch never disagree."""
    found = []
    if issue.state != "open":
        found.append(dict(verdict="closed", why=f"#{issue.number} is {issue.state}."))
    if issue.tickets:
        states = [(t, _ticket_state(t, issue)) for t in issue.tickets]
        listing = "\n".join(f"  #{t.number} {t.title} — {state}" for t, state in states)
        found.append(dict(
            verdict="spec", tickets=issue.tickets,
            run_instead=[t for t, state in states if state == "ready"],
            why=f"#{issue.number} is a spec split into {len(issue.tickets)} "
                f"ticket(s). Run a ticket instead:\n{listing}"))
    waiting = [b for b in issue.blockers if b.state == "open"]
    if waiting:
        listing = "\n".join(f"  #{b.number} {b.title}" for b in waiting)
        found.append(dict(
            verdict="blocked", blocked_by=waiting,
            why=f"#{issue.number} is blocked by {len(waiting)} open issue(s); it can "
                f"start once they are closed:\n{listing}"))
    if not force and RUNNING_LABEL in issue.labels:
        found.append(dict(
            verdict="claimed",
            why=f"#{issue.number} is labelled `{RUNNING_LABEL}`: another run has it. "
                "If that run is dead (a hard kill leaves the label), pass --force."))
    if not force and issue.open_prs:
        found.append(dict(
            verdict="open_pr", prs=issue.open_prs,
            why=f"an open PR already closes #{issue.number}: "
                f"{', '.join(issue.open_prs)}. Pass --force to run it again anyway."))
    return found


def _ticket_state(ticket: IssueLink, spec: Issue) -> str:
    if ticket.state != "open":
        return ticket.state
    return "ready" if spec.ready_label in ticket.labels else f"not labelled {spec.ready_label}"


# ── list the Ready ones ──────────────────────────────────────────────────────

def list_ready() -> dict:
    """This repo's open Ready issues, each with the verdict a Launch would reach.

    Never raises for a repo whose issues it cannot reach: `available` is false
    and `unavailable` says why in one line, and what to do about it.
    """
    label = ready_label()
    report = dict(tracker="GitHub",              # the only Tracker sssf reads, for now
                  repo=None, ready_label=label, available=False,
                  unavailable=None, issues=[], truncated=False)
    try:
        repo = report["repo"] = github_repo()
        page = _api(f"repos/{_slug(repo)}/issues", repo, "-f", "state=open",
                    "-f", f"labels={label}", "-F", f"per_page={LIST_PAGE}")
        found = [raw for raw in page if "pull_request" not in raw]   # PRs share the endpoint
        with ThreadPoolExecutor(max_workers=8) as pool:
            report["issues"] = list(pool.map(lambda raw: _listed(repo, raw, label),
                                             found[:LIST_LIMIT]))
    except Unavailable as why:
        report["unavailable"] = dict(reason=why.reason, fix=why.fix)
        return report
    except (RuntimeError, json.JSONDecodeError) as error:
        report["unavailable"] = dict(
            reason=f"cannot read the issues on {report['repo']}: {' '.join(str(error).split())}",
            fix="Check `gh auth status` and your connection, then refresh.")
        return report
    # One page: a full one may have had more behind it.
    report["truncated"] = len(found) > LIST_LIMIT or len(page) == LIST_PAGE
    _only_runnable_tickets(report["issues"])
    report["available"] = True
    return report


def _only_runnable_tickets(listed: list[dict]) -> None:
    """Narrow each Spec's `run_instead` to Tickets the listing itself found Runnable.

    `problems` can only see a Ticket's labels; whether it is blocked or has an
    open PR is in its own row. A Ticket past the listing's end keeps its place.
    """
    verdicts = {item["number"]: item["verdict"] for item in listed}
    for item in listed:
        item["run_instead"] = [t for t in item["run_instead"]
                               if verdicts.get(t["number"], "runnable") == "runnable"]


def _listed(repo: str, raw: dict, label: str) -> dict:
    """One Ready issue as --list-ready reports it: the first of its problems, if any."""
    body = _text(raw.get("body"))
    issue = Issue(repo=repo, number=raw["number"], title=raw["title"], url=raw["html_url"],
                  state=raw["state"], body=body, labels=_labels(raw), ready_label=label,
                  **_verdict_fields(repo, raw, body))
    first = next(iter(problems(issue)), {})

    def links(key: str) -> list[dict]:
        return [link.model_dump() for link in first.get(key, [])]

    return dict(number=issue.number, title=issue.title, url=issue.url,
                verdict=first.get("verdict", "runnable"), why=first.get("why"),
                blocked_by=links("blocked_by"), tickets=links("tickets"),
                run_instead=links("run_instead"), prs=first.get("prs", []))


# ── the prompt ───────────────────────────────────────────────────────────────

def as_prompt(issue: Issue) -> str:
    """The issue as a request every agent in the chain reads the same way."""
    parts = [f"# GitHub issue #{issue.number}: {issue.title}\n\n{issue.url}\n\n"
             f"This issue is the request. Build what it asks for, nothing more.\n\n{issue.body}"]
    if issue.parent:
        parts.append(f"# Background: parent spec #{issue.parent.number}: {issue.parent.title}\n\n"
                     f"Context only. The request is #{issue.number} above, not the whole spec: "
                     "use the spec's vocabulary and its decisions, and put tests where its "
                     f"Testing Decisions say.\n\n{issue.parent_body}")
    if issue.comments:
        parts.append(f"# Maintainer comments on #{issue.number}\n\nLater than the body. Where "
                     "they disagree, the comment is the correction.\n\n"
                     + "\n\n".join(issue.comments))
    if issue.checklist:
        parts.append(f"# {CHECKLIST_HEADING}\n\nFrom the issue's {issue.checklist_source}. The "
                     "review rules on each item by name, and every one must be met.\n\n"
                     + "\n".join(f"- {item}" for item in issue.checklist))
    return "\n\n---\n\n".join(parts)


def checklist_in(prompt: str) -> list[str]:
    """The `# Review checklist` items of a prompt — an issue's, or a hand-written one's."""
    return _items(_section(prompt, CHECKLIST_HEADING))


# ── claim it, and settle it ──────────────────────────────────────────────────

def claim(run, opts: RunOptions) -> None:
    """The `claim` phase: mark the issue as worked on; settle it when the run settles.

    Call right after the request phase. A request that is not an issue does nothing.
    """
    if not opts.issue:
        return
    issue = opts.issue
    with run.phase(PhaseParams(
            name="claim", kind="code", owner="github",
            description=f"Label #{issue.number} {RUNNING_LABEL} so no second run takes it, "
                        "and report back on it when this one ends")) as ph:
        _gh(["--method", "POST", f"repos/{_slug(issue.repo)}/labels", "-f", f"name={RUNNING_LABEL}",
             "-f", "color=FBCA04", "-f", "description=An SSSF run is working on this issue"],
            issue.repo)                  # 422 when it exists already; either way it exists now
        _api(f"repos/{_slug(issue.repo)}/issues/{issue.number}/labels", issue.repo,
             "-f", f"labels[]={RUNNING_LABEL}", method="POST")
        run.when_settled(lambda ok: _release(run, opts, ok))
        ph.log(issue=issue.url, label=RUNNING_LABEL,
               checklist=(f"{len(issue.checklist)} item(s) from {issue.checklist_source}"
                          if issue.checklist else "none — the review reads the whole issue"))


def _release(run, opts: RunOptions, ok: bool) -> None:
    issue = opts.issue
    _unclaim(issue.repo, issue.number, _outcome(run, opts, ok))
    run.console.note(f"#{issue.number}: {RUNNING_LABEL} removed, outcome commented")


def _unclaim(repo: str, number: int, comment: str) -> None:
    path = f"repos/{_slug(repo)}/issues/{number}"
    _api_optional(f"{path}/labels/{RUNNING_LABEL}", repo, method="DELETE")
    _api(f"{path}/comments", repo, "-f", f"body={comment}", method="POST")


def release_killed(issue_url: str, adw_id: str, adw: str) -> None:
    """Settle the claim of a Run that was killed before it could (procs.stop).

    What its own settle would have said, short of what died with it (its
    report, its spend): it was stopped, the ready label is still on, and how
    to rerun — picking its kept worktree back up when it has one.
    """
    repo, number = parse_ref(issue_url)
    kept = worktree.path_for(git_helper.repo_root(), adw_id).is_dir()
    rerun = (f"Its worktree is kept, and rerunning picks it back up:\n"
             f"`uv run adws/{adw}.py \"#{number}\" --adw-id {adw_id}`" if kept
             else f"Rerun it with:\n`uv run adws/{adw}.py \"#{number}\"`")
    _unclaim(repo, number, "\n\n".join([
        MARKER,
        f"⏹️ **SSSF run `{adw_id}`** (`{adw}`) was stopped, and killed before it could "
        "report back.",
        f"`{ready_label()}` is still on the issue. {rerun}"]))


def _outcome(run, opts: RunOptions, ok: bool) -> str:
    """The comment a settled run leaves on its issue."""
    adw = Path(sys.argv[0]).stem
    who = f"**SSSF run `{run.adw_id}`** (`{adw}`)"
    failed = next((p for p in reversed(run.phases) if p.status == "fail"), None)
    error = (failed.error or "") if failed else ""
    if ok:
        lines = [f"✅ {who} finished, and its work was accepted."]
    elif run.stopped:                    # stopped, not broken: a kill, Ctrl+C, the Console's Stop
        during = f" during `{failed.params.name}`" if failed else ""
        lines = [f"⏹️ {who} was stopped{during}."]
    elif failed:
        lines = [f"❌ {who} failed in `{failed.params.name}`.",
                 f"```\n{error[-TAIL_CHARS:]}\n```"]
    else:
        lines = [f"❌ {who} finished, but its work was not accepted: "
                 f"{run.not_accepted or 'the acceptance criterion was not met'}."]
    if run.landed.get("pr"):
        lines.append(f"Pull request: {run.landed['pr']}")
    elif run.landed.get("branch"):
        lines.append(f"Branch: `{run.landed['branch']}`")
    lines += run.report.values()
    if not ok and run.landed.get("pr"):
        # Failed after landing (its CI went red): the worktree is gone and the
        # open PR makes require_runnable refuse a plain rerun.
        lines.append(f"`{opts.issue.ready_label}` is still on the issue. Fix the pull request, "
                     "or close it and start a fresh run:\n"
                     f"`uv run adws/{adw}.py \"#{opts.issue.number}\" --force`")
    elif not ok:
        kept = " Its worktree is kept, and rerunning picks it back up:" if run.worktree else ""
        lines.append(f"`{opts.issue.ready_label}` is still on the issue.{kept}\n"
                     f"`uv run adws/{adw}.py \"#{opts.issue.number}\" --adw-id {run.adw_id}`")
    lines.append(f"{run.tokens:,} tokens · ${run.cost:.2f}")
    return "\n\n".join([MARKER, *lines])


# ── markdown ─────────────────────────────────────────────────────────────────

def _section(body: str, title: str) -> str:
    """The text under the first heading named `title` (any level, any case), up to
    the next heading at that level or above. Headings inside code fences don't count."""
    wanted = _title_key(title)
    level, fenced, out = None, False, []
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if level is None:
            if match and _title_key(match.group(2)) == wanted:
                level = len(match.group(1))
            continue
        if match and len(match.group(1)) <= level:
            break
        out.append(line)
    return "\n".join(out)


def _title_key(text: str) -> str:
    return " ".join(text.replace("*", "").strip().rstrip(":").casefold().split())


def _items(text: str) -> list[str]:
    """Top-level list items, checkboxes stripped. Indented lines — a wrapped line
    or a nested bullet — belong to the item above; prose ends the list's reach."""
    items: list[str] = []
    base = None
    attached = False
    for line in text.splitlines():
        match = _ITEM.match(line)
        indent = len(match.group(1).expandtabs(4)) if match else None
        if match and (base is None or indent <= base):
            base = indent if base is None else base
            items.append(match.group(2))
            attached = True
        elif line.strip() and attached and (match or line[0].isspace()):
            items[-1] += " " + (match.group(2) if match else line.strip())
        elif line.strip():
            attached = False             # prose between or after the list
    return items


def _refs(text: str, repo: str) -> list[int]:
    """Issue numbers `text` points at: `#12`, or a URL to an issue in `repo`."""
    host, slug = _split(repo)
    numbers = [int(n) for n in re.findall(r"(?<![\w/&])#(\d+)\b", text)]
    numbers += [int(n) for n in re.findall(
        rf"https?://{re.escape(host or 'github.com')}/{re.escape(slug)}/issues/(\d+)", text, re.I)]
    return list(dict.fromkeys(numbers))


def _text(value) -> str:
    return (value or "").replace("\r\n", "\n")


# ── gh ───────────────────────────────────────────────────────────────────────

def _split(repo: str) -> tuple[str | None, str]:
    """`[HOST/]OWNER/REPO` → (host or None for github.com, `OWNER/REPO`)."""
    parts = repo.split("/")
    return (parts[0], "/".join(parts[1:])) if len(parts) == 3 else (None, repo)


def _slug(repo: str) -> str:
    return _split(repo)[1]


def _link(raw: dict) -> IssueLink:
    return IssueLink(number=raw["number"], title=raw.get("title", ""),
                     url=raw.get("html_url", ""), state=raw.get("state", "open"),
                     labels=_labels(raw))


def _labels(raw: dict) -> list[str]:
    return [label["name"] if isinstance(label, dict) else label
            for label in raw.get("labels") or []]


def _gh(args: list[str], repo: str) -> subprocess.CompletedProcess:
    host, _ = _split(repo)
    env = operator_env()
    # Resolved the way `load` checks for it: on Windows a bare "gh" finds only gh.exe.
    gh = shutil.which("gh", path=env.get("PATH")) or "gh"
    argv = [gh, "api", *(["--hostname", host] if host else []), *args]
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _api(path: str, repo: str, *fields: str, method: str = "GET"):
    """One REST (or graphql) call; its JSON, or None for an empty reply. Raises on failure."""
    done = _gh(["--method", method, path, *fields], repo)
    if done.returncode != 0:
        raise RuntimeError(f"gh api {method} {path}: {done.stderr.strip()[-TAIL_CHARS:]}")
    return json.loads(done.stdout) if done.stdout.strip() else None


def _api_optional(path: str, repo: str, *fields: str, method: str = "GET"):
    """A call whose failure means "none": no parent, feature not enabled here."""
    try:
        return _api(path, repo, *fields, method=method)
    except (RuntimeError, json.JSONDecodeError):
        return None


def _api_list(path: str, repo: str) -> list[dict]:
    """Every item of a paginated list endpoint."""
    done = _gh(["--paginate", "--slurp", path], repo)
    if done.returncode != 0:
        raise RuntimeError(f"gh api {path}: {done.stderr.strip()[-TAIL_CHARS:]}")
    return [item for page in json.loads(done.stdout or "[]") for item in page]


# ── the command line ─────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """The command line. It prints: no Run is here to report through."""
    parser = argparse.ArgumentParser(prog="issues.py", description="This repo's issues.")
    parser.add_argument("--list-ready", action="store_true", required=True,
                        help=f"list up to {LIST_LIMIT} open Ready issues, each with its verdict")
    parser.add_argument("--json", action="store_true", help="print the listing as JSON")
    args = parser.parse_args(argv)
    listing = list_ready()
    if args.json:
        print(json.dumps(listing))
    elif not listing["available"]:
        print(f"{listing['unavailable']['reason']}. {listing['unavailable']['fix']}")
    else:
        for item in listing["issues"]:
            print(f"#{item['number']} {item['title']} — {item['verdict']}")
        if listing["truncated"]:
            print(f"(only the first {LIST_LIMIT} are listed)")
    return 0
