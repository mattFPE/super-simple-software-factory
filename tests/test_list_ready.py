"""Listing a repo's Ready issues, each with its verdict, from the command line (#12).

`adw_modules/issues.py --list-ready --json` runs for real in a temporary git
repo, with a stub `gh` first on PATH that answers from a fixture of GitHub's
replies. It is what the Console calls; it never asks GitHub itself.

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
ISSUES = SKILL / "templates" / "adws" / "adw_modules" / "issues.py"
REPO = "acme/widgets"
READY = "agent-ready"                  # this repo's name for the ready-for-agent role

TRIAGE_LABELS = f"""# Triage Labels

| Label in mattpocock/skills | Label in our tracker | Meaning |
| -------------------------- | -------------------- | ------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue |
| `ready-for-agent`          | `{READY}`            | Fully specified, ready for an AFK agent |
"""

# A GitHub CLI that answers from the fixture at $FAKE_GH_DATA:
#   logged_in   hosts `gh auth status --hostname` succeeds for
#   routes      REST path (query string dropped) -> JSON reply; a miss is a 404
#   closing     issue number -> URLs of open PRs that close it (the graphql query)
FAKE_GH = r'''
import json, os, sys
data = json.load(open(os.environ["FAKE_GH_DATA"], encoding="utf-8"))
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    host = args[args.index("--hostname") + 1] if "--hostname" in args else "github.com"
    sys.exit(0 if host in data.get("logged_in", []) else 1)
if args[0] != "api":
    sys.exit(f"fake gh: no {args[0]}")
words, i = [], 1
while i < len(args):
    if args[i] in ("--method", "-X", "--hostname", "-f", "-F"):
        words.append((args[i], args[i + 1])); i += 2
    else:
        words.append((None, args[i])); i += 1
path = next(w for flag, w in words if flag is None and not w.startswith("--"))
fields = dict(w.split("=", 1) for flag, w in words if flag in ("-f", "-F"))
if path == "graphql":
    urls = data.get("closing", {}).get(fields["n"], [])
    nodes = [{"url": u, "state": "OPEN"} for u in urls]
    print(json.dumps({"data": {"repository": {"issue": {
        "closedByPullRequestsReferences": {"nodes": nodes}}}}}))
    sys.exit(0)
reply = data["routes"].get(path.split("?")[0])
if reply is None:
    sys.exit("gh: Not Found (HTTP 404)")
if path.endswith("/issues"):
    wanted = fields.get("labels")
    reply = [r for r in reply if not wanted or wanted in [l["name"] for l in r["labels"]]]
    reply = reply[:int(fields.get("per_page", 30))]
print(json.dumps([reply] if "--slurp" in args else reply))
'''


def issue(number: int, title: str, *labels: str, body: str = "", state: str = "open",
          **extra) -> dict:
    return {"number": number, "title": title, "state": state, "body": body,
            "html_url": f"https://github.com/{REPO}/issues/{number}",
            "labels": [{"name": label} for label in labels], **extra}


class ListReadyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("remote", "add", "origin", f"https://github.com/{REPO}.git")
        (self.repo / "docs" / "agents").mkdir(parents=True)
        (self.repo / "docs" / "agents" / "triage-labels.md").write_text(TRIAGE_LABELS,
                                                                       encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
        if sys.platform == "win32":
            (self.bin / "gh.cmd").write_text(f'@"{sys.executable}" "%~dp0fake_gh.py" %*',
                                             encoding="utf-8")
        else:
            launcher = self.bin / "gh"
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" '
                                f'"$(dirname "$0")/fake_gh.py" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        self.data = {"logged_in": ["github.com"], "routes": {}, "closing": {}}

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def github(self, *issues: dict, **routes) -> None:
        """Open issues as GitHub lists them, each also readable on its own."""
        self.data["routes"][f"repos/{REPO}/issues"] = list(issues)
        for raw in issues:
            self.data["routes"][f"repos/{REPO}/issues/{raw['number']}"] = raw
        self.data["routes"].update(routes)

    def list_ready(self, path: str | None = None) -> dict:
        data = self.root / "gh.json"
        data.write_text(json.dumps(self.data), encoding="utf-8")
        env = {**os.environ, "PYTHONUTF8": "1", "FAKE_GH_DATA": str(data),
               "PATH": path if path is not None else f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        env.pop("PYTHONPATH", None)
        done = subprocess.run([sys.executable, str(ISSUES), "--list-ready", "--json"],
                              cwd=self.repo, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def by_number(self, listing: dict) -> dict[int, dict]:
        return {i["number"]: i for i in listing["issues"]}

    # ── verdicts ─────────────────────────────────────────────────────────────

    def test_lists_ready_issues_with_the_verdict_a_launch_would_reach(self):
        ticket = issue(1, "Add the widget", READY)
        blocked = issue(2, "Paint the widget", READY, body="## Blocked by\n\n- #1\n- #7\n")
        self.github(
            ticket, blocked,
            issue(3, "Widgets, the spec", READY, sub_issues_summary={"total": 4}),
            issue(4, "Polish the widget", READY, "agent-running"),
            issue(6, "Ship the widget", READY),
            issue(8, "Not triaged yet", "needs-triage"),
            issue(9, "A pull request", READY, pull_request={"url": "..."}),
            **{f"repos/{REPO}/issues/7": issue(7, "Closed prerequisite", state="closed"),
               f"repos/{REPO}/issues/3/sub_issues": [
                   ticket, blocked, issue(5, "Widget docs", state="closed"),
                   issue(10, "Widget tests", "needs-triage")]})
        self.data["closing"] = {"6": [f"https://github.com/{REPO}/pull/60"]}

        listing = self.list_ready()

        self.assertTrue(listing["available"], listing)
        self.assertEqual(listing["tracker"], "GitHub")
        self.assertEqual(listing["ready_label"], READY)
        self.assertFalse(listing["truncated"])
        issues = self.by_number(listing)
        self.assertEqual(sorted(issues), [1, 2, 3, 4, 6])
        self.assertEqual({n: i["verdict"] for n, i in issues.items()},
                         {1: "runnable", 2: "blocked", 3: "spec", 4: "claimed", 6: "open_pr"})
        self.assertEqual(issues[1]["title"], "Add the widget")
        self.assertEqual(issues[1]["url"], f"https://github.com/{REPO}/issues/1")
        self.assertIsNone(issues[1]["why"])
        # Blocked names only the blockers still open.
        self.assertEqual([b["number"] for b in issues[2]["blocked_by"]], [1])
        self.assertIn("blocked by 1 open issue", issues[2]["why"])
        # A spec names the tickets that can run instead: Runnable ones, so not #2.
        self.assertEqual([t["number"] for t in issues[3]["run_instead"]], [1])
        self.assertEqual([t["number"] for t in issues[3]["tickets"]], [1, 2, 5, 10])
        self.assertIn("Run a ticket instead", issues[3]["why"])
        self.assertIn("agent-running", issues[4]["why"])
        self.assertEqual(issues[6]["prs"], [f"https://github.com/{REPO}/pull/60"])
        self.assertIn("pull/60", issues[6]["why"])

    def test_at_most_fifty_are_listed_and_the_rest_flagged(self):
        self.github(*[issue(n, f"Ticket {n}", READY) for n in range(1, 53)])

        listing = self.list_ready()

        self.assertEqual(len(listing["issues"]), 50)
        self.assertTrue(listing["truncated"])

    # ── when issues are not available ────────────────────────────────────────

    def assert_unavailable(self, listing: dict, *said: str) -> None:
        self.assertFalse(listing["available"], listing)
        self.assertEqual(listing["issues"], [])
        reason = listing["unavailable"]
        for text in said:
            self.assertIn(text, reason["reason"] + " " + reason["fix"])
        self.assertNotIn("\n", reason["reason"])
        self.assertNotIn("\n", reason["fix"])

    def test_without_gh_on_path(self):
        git_dir = Path(shutil.which("git")).parent
        if shutil.which("gh", path=str(git_dir)):
            self.skipTest("gh is installed next to git")

        listing = self.list_ready(path=str(git_dir))

        self.assert_unavailable(listing, "`gh`", "PATH", "cli.github.com")

    def test_when_gh_is_not_logged_in(self):
        self.data["logged_in"] = []

        self.assert_unavailable(self.list_ready(), "not logged in", "gh auth login")

    def test_without_an_origin(self):
        self.git("remote", "remove", "origin")

        self.assert_unavailable(self.list_ready(), "`origin`", "git remote add origin")

    def test_when_origin_is_not_on_github(self):
        self.git("remote", "set-url", "origin", "https://gitlab.com/acme/widgets.git")

        self.assert_unavailable(self.list_ready(), "gitlab.com", "not on GitHub")

    def test_when_origin_is_not_a_repository_url(self):
        self.git("remote", "set-url", "origin", "/srv/git/widgets")

        self.assert_unavailable(self.list_ready(), "/srv/git/widgets", "not on GitHub")


if __name__ == "__main__":
    unittest.main()
