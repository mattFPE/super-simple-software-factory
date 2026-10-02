"""Reading a Local Markdown repo's issues: Specs and Tickets under `.scratch/` (#15).

Two seams, both against a temporary git repo holding a fixture `.scratch/`
tree: `adw_modules/issues.py --list-ready --json` and a real ADW refusing from
the terminal run as subprocesses; `load`, `require_runnable`, `as_prompt` and
`resolve_prompt` run in a child Python whose working directory is that repo,
since the Tracker is the engineer's checkout (ADR 0002).

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
ADWS = SKILL / "templates" / "adws"
ISSUES = ADWS / "adw_modules" / "issues.py"
READY = "agent-ready"                  # this repo's name for the ready-for-agent role

TRIAGE_LABELS = f"""# Triage Labels

| Label in mattpocock/skills | Label in our tracker | Meaning |
| -------------------------- | -------------------- | ------- |
| `ready-for-agent`          | `{READY}`            | Fully specified, ready for an AFK agent |
"""

LOCAL_TRACKER = """# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.
"""


def ticket(number: int, title: str, status: str | None, blocked_by: str | None = None) -> str:
    lines = [f"# {number:02d} — {title}", "",
             f"**What to build:** {title.lower()}, end to end.", ""]
    if blocked_by is not None:
        lines += [f"**Blocked by:** {blocked_by}", ""]
    if status is not None:
        lines += [f"**Status:** {status}", ""]
    lines += [f"- [ ] {title} works", f"- [ ] {title} is tested", ""]
    return "\n".join(lines)


SPEC = f"""# Widgets

Status: {READY}

## Problem Statement

People need widgets.

## User Stories

1. As a user, I want a widget, so that I have one.
"""

COMMENTS = """
## Comments

**Matt** (2026-10-01): use the blue paint, not the red.

---

<!-- sssf -->
❌ **SSSF run `cafebabe`** (`adw_plan_build`) failed in `build`.

---

### Matt, later

Also: two coats.
"""


class LocalIssuesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("docs/agents/triage-labels.md", TRIAGE_LABELS)
        self.write("docs/agents/issue-tracker.md", LOCAL_TRACKER)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def widgets(self) -> None:
        """A feature split into Tickets, one per verdict, and an unsplit one."""
        self.write(".scratch/widgets/spec.md", SPEC)
        issues = ".scratch/widgets/issues"
        self.write(f"{issues}/01-add-the-widget.md", ticket(1, "Add the widget", "resolved"))
        self.write(f"{issues}/02-paint-the-widget.md",
                   ticket(2, "Paint the widget", READY, blocked_by="01") + COMMENTS)
        self.write(f"{issues}/03-polish-the-widget.md",
                   ticket(3, "Polish the widget", READY, blocked_by="paint THE widget"))
        self.write(f"{issues}/04-ship-the-widget.md",
                   ticket(4, "Ship the widget", READY, blocked_by="01, 09"))
        self.write(f"{issues}/05-document-the-widget.md",
                   ticket(5, "Document the widget", "agent-running"))
        self.write(f"{issues}/06-triage-me.md", ticket(6, "Triage me", "needs-triage"))
        self.write(f"{issues}/07-test-the-widget.md",
                   ticket(7, "Test the widget", READY,
                          blocked_by="None — can start immediately"))
        self.write(".scratch/gadgets/spec.md", SPEC.replace("# Widgets", "# Gadgets"))

    def run_py(self, *argv: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONUTF8": "1"}
        env.pop("PYTHONPATH", None)
        return subprocess.run([sys.executable, *argv], cwd=self.repo, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def list_ready(self) -> dict:
        done = self.run_py(str(ISSUES), "--list-ready", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def module(self, code: str):
        """Run `code` against adw_modules in the repo; what it prints as JSON, or the
        SystemExit message it refused with (as {"refused": ...})."""
        script = "\n".join([
            "import json, sys",
            f"sys.path.insert(0, {str(ADWS)!r})",
            "from adw_modules import issues, utils",
            "try:",
            "    result = None",
            textwrap.indent(textwrap.dedent(code), "    "),
            "except SystemExit as stop:",
            "    result = {'refused': str(stop)}",
            "print(json.dumps(result))"])
        done = self.run_py("-c", script)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def load(self, ref: str) -> dict:
        return self.module(f"""\
            issue = issues.load({ref!r})
            problems = issues.problems(issue)
            result = dict(issue.model_dump(), ref=issue.ref,
                          verdicts=[p["verdict"] for p in problems],
                          why="\\n".join(p["why"] for p in problems))
            """)

    # ── which Tracker ────────────────────────────────────────────────────────

    def test_the_tracker_comes_from_the_issue_tracker_heading(self):
        self.widgets()
        listing = self.list_ready()
        self.assertTrue(listing["available"], listing)
        self.assertEqual(listing["tracker"], "Local Markdown")
        self.assertEqual(listing["ready_label"], READY)

    def test_an_unknown_tracker_heading_means_issues_are_unavailable(self):
        self.write("docs/agents/issue-tracker.md", "# Issue tracker: Linear\n\nIssues are in Linear.\n")

        listing = self.list_ready()

        self.assertFalse(listing["available"], listing)
        self.assertEqual(listing["issues"], [])
        said = listing["unavailable"]["reason"] + " " + listing["unavailable"]["fix"]
        self.assertIn("Linear", said)
        self.assertIn("Local Markdown", said)
        self.assertNotIn("\n", said)

    def test_without_an_issue_tracker_file_the_tracker_is_github(self):
        (self.repo / "docs" / "agents" / "issue-tracker.md").unlink()

        listing = self.list_ready()

        self.assertEqual(listing["tracker"], "GitHub")

    # ── --list-ready ─────────────────────────────────────────────────────────

    def test_lists_local_ready_issues_with_the_verdict_a_launch_would_reach(self):
        self.widgets()

        listing = self.list_ready()

        self.assertFalse(listing["truncated"])
        issues = {i["path"]: i for i in listing["issues"]}
        t = ".scratch/widgets/issues/"
        self.assertEqual({path: i["verdict"] for path, i in issues.items()}, {
            ".scratch/gadgets/spec.md": "runnable",
            ".scratch/widgets/spec.md": "spec",
            f"{t}02-paint-the-widget.md": "runnable",
            f"{t}03-polish-the-widget.md": "blocked",
            f"{t}04-ship-the-widget.md": "blocked",
            f"{t}05-document-the-widget.md": "claimed",
            f"{t}07-test-the-widget.md": "runnable"})
        paint = issues[f"{t}02-paint-the-widget.md"]
        self.assertEqual((paint["number"], paint["title"]), (2, "Paint the widget"))
        self.assertEqual(paint["url"], f"{t}02-paint-the-widget.md")
        self.assertIsNone(paint["why"])
        # Matched by title, ignoring case; named by its path.
        polish = issues[f"{t}03-polish-the-widget.md"]
        self.assertEqual([b["path"] for b in polish["blocked_by"]], [f"{t}02-paint-the-widget.md"])
        self.assertIn("02-paint-the-widget.md", polish["why"])
        # 01 is resolved, so only the reference that matches nothing still blocks.
        ship = issues[f"{t}04-ship-the-widget.md"]
        self.assertEqual(len(ship["blocked_by"]), 1)
        self.assertIn("09", ship["why"])
        self.assertIn("matches no Ticket", ship["why"])
        # A Spec with Tickets names the Runnable ones to run instead.
        spec = issues[".scratch/widgets/spec.md"]
        self.assertEqual([r["path"] for r in spec["run_instead"]],
                         [f"{t}02-paint-the-widget.md", f"{t}07-test-the-widget.md"])
        self.assertEqual(len(spec["tickets"]), 7)
        self.assertIn("Run a ticket instead", spec["why"])
        self.assertIn("agent-running", issues[f"{t}05-document-the-widget.md"]["why"])

    def test_at_most_fifty_are_listed_and_the_rest_flagged(self):
        for n in range(1, 53):
            self.write(f".scratch/many/issues/{n:02d}-ticket.md", ticket(n, f"Ticket {n}", READY))

        listing = self.list_ready()

        self.assertEqual(len(listing["issues"]), 50)
        self.assertTrue(listing["truncated"])

    def test_a_repo_with_no_scratch_folder_has_no_ready_issues(self):
        listing = self.list_ready()
        self.assertTrue(listing["available"], listing)
        self.assertEqual(listing["issues"], [])

    # ── load and require_runnable ────────────────────────────────────────────

    def test_a_ticket_reads_with_its_spec_comments_and_checklist(self):
        self.widgets()

        issue = self.load(".scratch/widgets/issues/02-paint-the-widget.md")

        self.assertEqual(issue["verdicts"], [])
        self.assertEqual(issue["ref"], ".scratch/widgets/issues/02-paint-the-widget.md")
        self.assertEqual(issue["title"], "Paint the widget")
        self.assertEqual(issue["parent"]["path"], ".scratch/widgets/spec.md")
        self.assertIn("People need widgets.", issue["parent_body"])
        self.assertEqual(issue["checklist"], ["Paint the widget works", "Paint the widget is tested"])
        # Every comment but sssf's own outcome comment, and none left in the body.
        self.assertEqual(len(issue["comments"]), 2)
        self.assertIn("blue paint", issue["comments"][0])
        self.assertIn("two coats", issue["comments"][1])
        self.assertNotIn("cafebabe", json.dumps(issue["comments"]) + issue["body"])
        self.assertNotIn("blue paint", issue["body"])

    def test_the_prompt_carries_the_ticket_its_spec_and_its_comments(self):
        self.widgets()

        prompt = self.module("""\
            result = utils.resolve_prompt(".scratch/widgets/issues/02-paint-the-widget.md")
            """)

        self.assertIn(".scratch/widgets/issues/02-paint-the-widget.md", prompt)
        self.assertIn("Paint the widget", prompt)
        self.assertIn("parent spec", prompt)
        self.assertIn("People need widgets.", prompt)
        self.assertIn("blue paint", prompt)
        self.assertIn("# Review checklist", prompt)
        self.assertNotIn("cafebabe", prompt)

    def test_a_backslashed_path_names_the_same_issue(self):
        self.widgets()

        issue = self.load(".scratch\\widgets\\issues\\02-paint-the-widget.md")

        self.assertEqual(issue["ref"], ".scratch/widgets/issues/02-paint-the-widget.md")

    def test_a_blocker_clears_only_when_resolved(self):
        self.widgets()
        self.write(".scratch/widgets/issues/01-add-the-widget.md",
                   ticket(1, "Add the widget", "ready-for-human"))

        issue = self.load(".scratch/widgets/issues/02-paint-the-widget.md")

        self.assertEqual(issue["verdicts"], ["blocked"])
        self.assertIn("01-add-the-widget.md", issue["why"])

    def test_every_blocker_in_a_prose_list_counts(self):
        self.widgets()
        self.write(".scratch/widgets/issues/08-last.md",
                   ticket(8, "Last", READY, blocked_by="01 and 02"))

        issue = self.load(".scratch/widgets/issues/08-last.md")

        self.assertEqual(issue["verdicts"], ["blocked"])
        self.assertIn("02-paint-the-widget.md", issue["why"])

    def test_a_missing_blocked_by_line_means_unblocked(self):
        self.widgets()
        self.write(".scratch/widgets/issues/08-free.md", ticket(8, "Free", READY))

        self.assertEqual(self.load(".scratch/widgets/issues/08-free.md")["verdicts"], [])

    def test_a_spec_with_tickets_is_refused_naming_the_runnable_ones(self):
        self.widgets()

        refused = self.module("""\
            issues.require_runnable(issues.load(".scratch/widgets/spec.md"))
            """)["refused"]

        self.assertIn("Run a ticket instead", refused)
        self.assertIn("02-paint-the-widget.md", refused)

    def test_a_spec_without_tickets_runs_as_its_own_ticket(self):
        self.widgets()

        issue = self.load(".scratch/gadgets/spec.md")

        self.assertEqual(issue["verdicts"], [])
        self.assertIsNone(issue["parent"])
        self.assertEqual(issue["checklist_source"], "User Stories")

    def test_a_ticket_without_the_ready_status_does_not_become_a_prompt(self):
        self.widgets()

        refused = self.module("""\
            utils.resolve_prompt(".scratch/widgets/issues/06-triage-me.md")
            """)["refused"]

        self.assertIn("needs-triage", refused)
        self.assertIn(READY, refused)

    def test_a_scratch_path_that_names_no_file_is_refused(self):
        self.widgets()

        refused = self.module("""\
            issues.load(".scratch/widgets/issues/99-nope.md")
            """)["refused"]

        self.assertIn("99-nope.md", refused)

    def test_a_scratch_path_in_a_github_repo_stays_a_request_file(self):
        self.widgets()
        self.write("docs/agents/issue-tracker.md", "# Issue tracker: GitHub\n")

        prompt = self.module("""\
            result = utils.resolve_prompt(".scratch/widgets/issues/06-triage-me.md")
            """)

        self.assertEqual(prompt, ticket(6, "Triage me", "needs-triage"))

    def test_a_scratch_path_under_an_unknown_tracker_is_refused(self):
        self.widgets()
        self.write("docs/agents/issue-tracker.md", "# Issue tracker: Linear\n")

        refused = self.module("""\
            utils.resolve_prompt(".scratch/widgets/issues/06-triage-me.md")
            """)["refused"]

        self.assertIn("Linear", refused)

    def test_a_github_reference_in_a_local_markdown_repo_is_refused(self):
        refused = self.module("""\
            issues.load("#42")
            """)["refused"]

        self.assertIn("Local Markdown", refused)
        self.assertIn(".scratch/", refused)

    # ── an ADW from the terminal ─────────────────────────────────────────────

    def test_an_adw_refuses_an_unrunnable_local_issue_with_the_reason(self):
        self.widgets()

        done = self.run_py(str(ADWS / "adw_plan_build.py"),
                           ".scratch/widgets/issues/03-polish-the-widget.md")

        self.assertNotEqual(done.returncode, 0)
        self.assertIn("blocked by", done.stderr)
        self.assertIn("02-paint-the-widget.md", done.stderr)


if __name__ == "__main__":
    unittest.main()
