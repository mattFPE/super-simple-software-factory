"""Every ADW checks an argv with its own parser under --check-args, and starts nothing (#25).

Each ADW runs for real from an empty directory, as in test_adw_describe: no
config, no git repo, no agent, and here no `gh` either. A check that started
anything would fail there or leave something behind.

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_adw_describe import ADWS, COMMITTING, CUSTOM_ADW, MAKE_ADW, TEMPLATES_ADWS, describe

# An argv every committing ADW refuses, and the words its refusal says.
REFUSED = [
    (["--branch", "--merge", "x"], "not allowed with argument --branch"),
    (["--merge"], "the following arguments are required: prompt"),
    (["--allow-dirty", "x"], "--allow-dirty only applies with --in-place"),
    (["--in-place", "--merge", "x"], "--merge / --pr end a worktree run"),
    (["--in-place", "--pr", "x"], "--merge / --pr end a worktree run"),
    (["--force", "add a health endpoint"], "--force only applies when the prompt is an issue"),
    # An issue's run refuses these before it asks GitHub for the issue.
    (["#42", "--allow-dirty"], "--allow-dirty only applies with --in-place"),
    (["#42", "--in-place", "--merge"], "--merge / --pr end a worktree run"),
]


def run(script: Path, cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    # PATH holds nothing: an ADW that asked git or gh anything would fail for it.
    env = {**os.environ, "PYTHONPATH": str(TEMPLATES_ADWS), "PYTHONUTF8": "1", "PATH": str(cwd)}
    return subprocess.run([sys.executable, str(script), *argv], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8")


class CheckArgsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assertAccepts(self, script: Path, *argv: str):
        done = run(script, self.cwd, "--check-args", *argv)
        self.assertEqual((done.returncode, done.stdout.strip(), done.stderr), (0, "ok", ""),
                         f"{script.name} --check-args {argv}")

    def test_every_template_adw_accepts_a_valid_argv_without_starting_anything(self):
        for adw in ADWS:
            with self.subTest(adw=adw):
                script = TEMPLATES_ADWS / f"{adw}.py"
                self.assertAccepts(script, "--adw-id", "beefbeef", "--", "add a health endpoint")
                if adw in COMMITTING:
                    self.assertAccepts(script, "--in-place", "--allow-dirty", "--", "x")
        # No config read, no session dir, no trace db, no worktree: nothing was written.
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_an_issue_prompt_is_checked_without_asking_github(self):
        for adw in sorted(COMMITTING):
            with self.subTest(adw=adw):
                self.assertAccepts(TEMPLATES_ADWS / f"{adw}.py", "#42", "--merge", "--force",
                                   "--adw-id", "beefbeef")
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_a_committing_adw_refuses_what_a_real_run_refuses_with_the_same_message(self):
        for adw in sorted(COMMITTING):
            script = TEMPLATES_ADWS / f"{adw}.py"
            for argv, said in REFUSED:
                with self.subTest(adw=adw, argv=argv):
                    checked = run(script, self.cwd, "--check-args", *argv)
                    real = run(script, self.cwd, *argv)
                    self.assertNotEqual(checked.returncode, 0)
                    self.assertIn(said, checked.stderr)
                    self.assertEqual((checked.returncode, checked.stderr),
                                     (real.returncode, real.stderr))
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_describe_says_the_adw_checks_args_and_hides_the_flag(self):
        for adw in ADWS:
            with self.subTest(adw=adw):
                d = describe(TEMPLATES_ADWS / f"{adw}.py", self.cwd)
                self.assertIs(d["checks_args"], True)
                self.assertNotIn("--check-args", [o["flag"] for o in d["options"]])

    def test_an_adw_written_with_the_shared_cli_setup_checks_its_own_options(self):
        script = self.cwd / "adw_custom.py"
        script.write_text(CUSTOM_ADW, encoding="utf-8")
        self.assertAccepts(script, "--depth", "deep", "--", "x")
        refused = run(script, self.cwd, "--check-args", "--depth", "sideways", "--", "x")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("invalid choice: 'sideways'", refused.stderr)
        self.assertIs(describe(script, self.cwd)["checks_args"], True)

    def test_an_adw_generated_by_make_adw_checks_args(self):
        subprocess.run([sys.executable, str(MAKE_ADW), "--name", "recon", "--agents", "scout"],
                       cwd=self.cwd, check=True, capture_output=True)
        script = self.cwd / "adws" / "adw_recon.py"
        before = sorted(p.relative_to(self.cwd) for p in self.cwd.rglob("*"))
        self.assertAccepts(script, "--adw-id", "beefbeef", "--", "x")
        self.assertEqual(sorted(p.relative_to(self.cwd) for p in self.cwd.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
