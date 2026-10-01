"""A joined run's plan commit holds the plan, never a crashed build's leftovers.

End to end through the real ADW: a throwaway target repo stamped by the
installer, every agent a fake `claude` that plays its role from a marker in its
system prompt, adw_simple_sdlc run twice under one --adw-id (#5).

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
ADW_ID = "deadbeef"
ROLES = ("planner", "builder", "reviewer", "documenter")

# Plays each agent. The planner writes the spec its run number names; the
# builder writes code and a test, then dies before it reports — the state a
# phase leaves behind when the agent crashes mid-build — unless FAKE_BUILD says
# this is the run that finishes. The suite imports the implementation, so it
# fails without it: the red check can tell a real baseline from a polluted one.
FAKE = r'''
import json, os, pathlib, sys
argv = sys.argv[1:]
system = pathlib.Path(argv[argv.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
role = system.split("SSSF-FAKE-ROLE: ", 1)[1].split()[0]
sys.stdin.read()
run = os.environ["FAKE_RUN"]

def finish(envelope):
    for event in ({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
                  {"type": "result", "subtype": "success", "is_error": False,
                   "result": "```json\n" + json.dumps(envelope) + "\n```", "modelUsage": {}}):
        print(json.dumps(event), flush=True)

if role == "planner":
    pathlib.Path("specs").mkdir(exist_ok=True)
    pathlib.Path("specs/plan.md").write_text(f"# Plan, run {run}\n", encoding="utf-8")
    finish({"status": "success", "summary": "planned", "artifacts": ["specs/plan.md"],
            "commit_message": f"Add spec, run {run}"})
elif role == "builder":
    pathlib.Path("src").mkdir(exist_ok=True)
    pathlib.Path("src/impl.py").write_text(f"VALUE = {run}\n", encoding="utf-8")
    pathlib.Path("tests").mkdir(exist_ok=True)
    pathlib.Path("tests/test_impl.py").write_text("from src.impl import VALUE\n", encoding="utf-8")
    if os.environ.get("FAKE_BUILD") != "finish":
        print(json.dumps({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}),
              flush=True)
        sys.exit(2)
    finish({"status": "success", "summary": "built", "commit_message": "Add the value",
            "changed_files": ["src/impl.py", "tests/test_impl.py"],
            "test_files": ["tests/test_impl.py"]})
else:
    sys.exit(f"the fake has no {role} in this scenario")
'''

CONFIG = """\
defaults:
  coding_agent: claude_code
  model: sonnet
  thinking: low
  tools: [read, bash, edit, write]
  data_dir: adws/adw_data
quality:
  test: [{python}, -c, "import src.impl"]
agents:
{agents}
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=repo, check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def fake_claude(directory: Path) -> Path:
    script = directory / "fake_claude.py"
    script.write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        launcher = directory / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


def target_repo(root: Path) -> Path:
    """A committed repo with sssf stamped in and every agent faked."""
    repo = root / "target"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("target\n", encoding="utf-8")
    subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")], cwd=repo,
                   check=True, capture_output=True, text=True, encoding="utf-8")
    agents = []
    for role in ROLES:
        prompts = Path("adws/adw_data/prompt_engineering") / role
        system = repo / prompts / "system.md"
        system.write_text(f"SSSF-FAKE-ROLE: {role}\n\n" + system.read_text(encoding="utf-8"),
                          encoding="utf-8")
        writes = "    writes: [specs/]\n" if role == "planner" else ""
        agents.append(f"  - name: {role}\n    prompt_engineering:\n"
                      f"      system: {(prompts / 'system.md').as_posix()}\n"
                      f"      user: {(prompts / 'user.md').as_posix()}\n{writes}")
    (repo / "adws/adw_sssf_config/sssf.config.yaml").write_text(
        CONFIG.format(python=Path(sys.executable).as_posix(), agents="".join(agents)),
        encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "stamp sssf")
    return repo


def run_adw(repo: Path, claude: Path, run: int, build: str = "crash") -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_CODE_PATH": str(claude), "FAKE_RUN": str(run), "FAKE_BUILD": build,
           "PYTHONUTF8": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(
        ["uv", "run", "adws/adw_simple_sdlc.py", "add a value", "--adw-id", ADW_ID],
        cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300)


@unittest.skipUnless(shutil.which("uv"), "needs uv to run an ADW")
class JoinedRunAfterACrashedBuild(unittest.TestCase):
    def test_the_plan_commit_holds_only_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            repo, claude = target_repo(tmp), fake_claude(tmp)

            first = run_adw(repo, claude, 1)
            self.assertNotEqual(first.returncode, 0, "the first run's build should crash")
            second = run_adw(repo, claude, 2)

            branch = f"sssf/{ADW_ID}"
            log = git(repo, "log", "--format=%H %s", f"main..{branch}").splitlines()
            plan_commit = next((sha for sha, _, subject in (l.partition(" ") for l in log)
                                if subject == "Add spec, run 2"), None)
            self.assertIsNotNone(plan_commit, f"no run-2 plan commit on {branch}:\n{log}\n"
                                              f"{second.stdout[-3000:]}\n{second.stderr[-3000:]}")
            files = git(repo, "show", "--name-only", "--format=", plan_commit).splitlines()
            self.assertEqual(files, ["specs/plan.md"])

    def test_a_rerun_can_finish_the_build_through_the_red_check(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            repo, claude = target_repo(tmp), fake_claude(tmp)

            run_adw(repo, claude, 1)
            second = run_adw(repo, claude, 2, build="finish")

            db = sqlite3.connect(repo / "adws/adw_data/sssf.db")
            try:
                red_checks = db.execute(
                    "select g.passed from gate_results g join phases p using (phase_id) "
                    "where p.adw_id = ? and p.name = 'build' and g.gate = "
                    "'tests_fail_without_change' order by g.id", (ADW_ID,)).fetchall()
            finally:
                db.close()
            self.assertEqual(red_checks[-1:], [(1,)],
                             f"{second.stdout[-3000:]}\n{second.stderr[-3000:]}")

    def test_a_rerun_that_writes_the_same_spec_still_gets_past_the_plan_commit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp = Path(tmp)
            repo, claude = target_repo(tmp), fake_claude(tmp)

            run_adw(repo, claude, 1)
            second = run_adw(repo, claude, 1)           # same run number: same spec

            db = sqlite3.connect(repo / "adws/adw_data/sssf.db")
            try:
                statuses = db.execute("select status from phases where adw_id = ? and "
                                      "name = 'commit_plan' order by seq", (ADW_ID,)).fetchall()
            finally:
                db.close()
            self.assertEqual(statuses, [("success",), ("success",)],
                             f"{second.stdout[-3000:]}\n{second.stderr[-3000:]}")


if __name__ == "__main__":
    unittest.main()
