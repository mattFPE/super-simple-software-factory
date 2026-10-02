"""Claiming and settling a local Ticket in the engineer's checkout (#16, ADR 0002).

A real Run, short of its agents: a stub ADW in a temporary Local Markdown repo
goes through the same lifecycle functions every committing ADW calls —
`issues.claim`, `worktree.enter`, `git_helper.commit_all`, `worktree.land`,
`run.finish` — with its one "agent" a code phase that writes a file, or breaks.
The permissions check and `release_killed` run in a child Python against the
same repo.

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
READY, HUMAN = "agent-ready", "human-ready"     # this repo's names for the two roles
MARKER = "<!-- sssf -->"
ADW_ID = "c1a1b0de"
PAINT = ".scratch/widgets/issues/02-paint-the-widget.md"
OTHER = ".scratch/widgets/issues/03-other.md"

TRIAGE_LABELS = f"""# Triage Labels

| Label in mattpocock/skills | Label in our tracker | Meaning |
| -------------------------- | -------------------- | ------- |
| `ready-for-agent`          | `{READY}`            | Fully specified, ready for an AFK agent |
| `ready-for-human`          | `{HUMAN}`            | Requires human implementation |
"""

LOCAL_TRACKER = "# Issue tracker: Local Markdown\n\nIssues live in `.scratch/`.\n"

TICKET = """# {number:02d} — {title}

**What to build:** {title}.

**Status:** {status}

- [ ] it works

## Comments

**Matt** (2026-10-01): use the blue paint.
"""

# A committing ADW with its agents taken out: the code phase in their place
# writes the widget, or breaks when STUB_FAIL is set. It records the Ticket's
# Status in the checkout as the Run sees it mid-run, so the Claim is observable.
STUB_ADW = r'''
import argparse, os, pathlib, sys
from adw_modules import git_helper, issues, session, utils, worktree
from adw_modules.data_types import PhaseParams, SSSFConfig

parser = argparse.ArgumentParser()
parser.add_argument("prompt")
session.add_cli_args(parser, commits=True)
args = parser.parse_args()
prompt = utils.resolve_prompt(args.prompt)
opts = session.cli_options(args)
cfg = SSSFConfig.model_validate({"defaults": {"data_dir": "adws/adw_data"},
                                 "observability": {"db": "adws/adw_data/sssf.db"}})
worktree.preflight(opts)
run = session.ensure(cfg, opts.adw_id)
with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                           description="Capture the incoming ask")) as ph:
    ph.log(input=prompt)
issues.claim(run, opts)
pathlib.Path(os.environ["STUB_SEEN"]).write_text(
    (run.main_root / opts.issue.path).read_text(encoding="utf-8"), encoding="utf-8")
worktree.enter(run, opts)
with run.phase(PhaseParams(name="build", kind="code", owner="builder",
                           description="Stand in for the agents")):
    (pathlib.Path(run.repo_root) / "widget.py").write_text("PAINT = 'blue'\n", encoding="utf-8")
    if os.environ.get("STUB_FAIL"):
        raise RuntimeError("the paint ran")
with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                           description="Commit the code")) as ph:
    ph.log(sha=git_helper.commit_all("Paint the widget", run.repo_root))
worktree.land(run, opts)
sys.exit(run.finish())
'''


def ticket(number: int, title: str, status: str) -> str:
    return TICKET.format(number=number, title=title, status=status)


class LocalClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write(".gitignore", "adws/adw_data/\n__pycache__/\n")
        self.write("docs/agents/triage-labels.md", TRIAGE_LABELS)
        self.write("docs/agents/issue-tracker.md", LOCAL_TRACKER)
        self.write(".scratch/widgets/spec.md", f"# Widgets\n\nStatus: {READY}\n")
        self.write(PAINT, ticket(2, "Paint the widget", READY))
        self.write(OTHER, ticket(3, "Other", READY))
        self.write("adws/adw_stub.py", STUB_ADW)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "widgets")
        self.seen = self.root / "seen.md"

    def tearDown(self):
        self.tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────

    def env(self) -> dict:
        env = {**os.environ, "PYTHONPATH": str(ADWS), "PYTHONUTF8": "1",
               "ENGINEER_NAME": "test", "STUB_SEEN": str(self.seen),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        env.pop("STUB_FAIL", None)
        return env

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              cwd=self.repo, check=True, capture_output=True, text=True,
                              encoding="utf-8").stdout.strip()

    def write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.repo / rel).read_text(encoding="utf-8")

    def status(self) -> list[str]:
        # Not self.git: its strip() would eat the leading space of " M path".
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.splitlines()

    def run_stub(self, *flags: str, fail: bool = False) -> subprocess.CompletedProcess:
        env = self.env()
        if fail:
            env["STUB_FAIL"] = "1"
        return subprocess.run([sys.executable, "adws/adw_stub.py", PAINT, "--adw-id", ADW_ID,
                               *flags], cwd=self.repo, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)

    def module(self, code: str):
        """Run `code` against adw_modules in the repo; what it leaves in `result`, as JSON."""
        script = "\n".join([
            "import json, sys",
            f"sys.path.insert(0, {str(ADWS)!r})",
            "from adw_modules import issues, permissions",
            "from adw_modules.data_types import AgentConfig, SSSFConfig",
            "PROMPTS = {'system': 's.md', 'user': 'u.md'}",
            "result = None",
            textwrap.dedent(code),
            "print(json.dumps(result))"])
        done = subprocess.run([sys.executable, "-c", script], cwd=self.repo, env=self.env(),
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def claim_other(self) -> None:
        """Another Run's Claim, pending in the checkout."""
        self.write(OTHER, ticket(3, "Other", "agent-running"))

    # ── claim ────────────────────────────────────────────────────────────────

    def test_the_claim_sets_agent_running_in_the_checkout(self):
        done = self.run_stub("--branch")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("**Status:** agent-running", self.seen.read_text(encoding="utf-8"))

    # ── settle ───────────────────────────────────────────────────────────────

    def test_a_failed_run_restores_the_ready_status_and_comments_uncommitted(self):
        head = self.git("rev-parse", "HEAD")

        done = self.run_stub(fail=True)

        self.assertNotEqual(done.returncode, 0)
        text = self.read(PAINT)
        self.assertIn(f"**Status:** {READY}", text)
        self.assertNotIn("agent-running", text)
        comments = text.split("## Comments", 1)[1]
        self.assertIn("blue paint", comments)          # the engineer's comment stays
        self.assertIn(MARKER, comments)
        self.assertIn(ADW_ID, comments)
        self.assertIn("the paint ran", comments)
        self.assertIn(f'uv run adws/adw_stub.py "{PAINT}" --adw-id {ADW_ID}', comments)
        # Uncommitted, in the checkout: HEAD has not moved and the file is modified.
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.status(), [f" M {PAINT}"])
        # sssf's own comment never reaches the next Run's agent.
        issue = self.module(f"result = issues.load({PAINT!r}).comments")
        self.assertEqual(len(issue), 1)
        self.assertIn("blue paint", issue[0])

    def test_a_local_issue_lands_with_merge_and_resolves_in_the_same_history(self):
        self.claim_other()

        done = self.run_stub()

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # The code and the Ticket's resolution are both on the engineer's branch.
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue((self.repo / "widget.py").is_file())
        text = self.read(PAINT)
        self.assertIn("**Status:** resolved", text)
        self.assertIn(MARKER, text)
        self.assertIn(ADW_ID, text)
        # It arrived by a commit of the Run's own branch that touches only the Ticket.
        resolving = self.git("log", "-1", "--format=%H", "--", PAINT)
        self.assertEqual(self.git("show", "--name-only", "--format=", resolving), PAINT)
        self.assertIn("**Status:** resolved", self.git("show", f"{resolving}:{PAINT}"))
        self.assertEqual(self.git("branch", "--contains", resolving).strip("* "), "main")
        # The other Run's pending Claim neither blocked the merge nor rode along.
        self.assertEqual(self.status(), [f" M {OTHER}"])
        self.assertIn("agent-running", self.read(OTHER))

    def test_a_branch_run_leaves_the_ticket_ready_for_human_naming_the_branch(self):
        head = self.git("rev-parse", "HEAD")

        done = self.run_stub("--branch")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = self.read(PAINT)
        self.assertIn(f"**Status:** {HUMAN}", text)
        self.assertIn(f"sssf/{ADW_ID}", text.split("## Comments", 1)[1])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.status(), [f" M {PAINT}"])
        self.assertIn("widget.py", self.git("show", "--name-only", "--format=", f"sssf/{ADW_ID}"))

    # ── in place ─────────────────────────────────────────────────────────────

    def test_an_in_place_run_is_not_refused_for_a_pending_claim_nor_commits_it(self):
        self.claim_other()

        done = self.run_stub("--in-place")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        code = self.git("log", "-1", "--format=%H", "--", "widget.py")
        self.assertEqual(self.git("show", "--name-only", "--format=", code), "widget.py")
        # The code is on the engineer's branch, so the Ticket is Resolved — in the
        # checkout, uncommitted: no Tracker commit is made on their branch.
        self.assertIn("**Status:** resolved", self.read(PAINT))
        self.assertIn(f"**Status:** {READY}", self.git("show", f"HEAD:{PAINT}"))
        self.assertIn("agent-running", self.read(OTHER))
        self.assertEqual(sorted(self.status()), [f" M {PAINT}", f" M {OTHER}"])

    # ── a stopped Run ────────────────────────────────────────────────────────

    def test_a_killed_runs_claim_is_released_in_the_checkout(self):
        self.write(PAINT, ticket(2, "Paint the widget", "agent-running"))

        self.module(f"issues.release_killed({PAINT!r}, {ADW_ID!r}, 'adw_stub')")

        text = self.read(PAINT)
        self.assertIn(f"**Status:** {READY}", text)
        self.assertIn(MARKER, text)
        self.assertIn("stopped", text)
        self.assertIn(f'uv run adws/adw_stub.py "{PAINT}"', text)

    # ── permissions ──────────────────────────────────────────────────────────

    def test_no_agent_may_edit_scratch_unless_a_writes_entry_names_it(self):
        allowed = self.module(f"""\
            cfg = SSSFConfig.model_validate({{}})
            def may(writes):
                agent = AgentConfig(name="builder", writes=writes, prompt_engineering=PROMPTS)
                return permissions.permitted({PAINT!r}, agent, cfg)
            result = [may(None), may(["**/*.md"]), may([".scratch/"]), may(["src/"])]
            """)

        self.assertEqual(allowed, [False, False, True, False])

    def test_in_a_github_repo_scratch_is_not_protected(self):
        self.write("docs/agents/issue-tracker.md", "# Issue tracker: GitHub\n")

        allowed = self.module(f"""\
            agent = AgentConfig(name="builder", writes=None, prompt_engineering=PROMPTS)
            result = permissions.permitted({PAINT!r}, agent, SSSFConfig.model_validate({{}}))
            """)

        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
