"""Every ADW describes its own command line with --describe, and starts nothing (#8).

Each template ADW runs for real from an empty directory: no config, no git
repo, no agent. An ADW that worked would fail there; one that only describes
itself exits 0 and leaves the directory as it found it.

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
TEMPLATES_ADWS = SKILL / "templates" / "adws"
MAKE_ADW = SKILL / "scripts" / "make_adw.py"
ADWS = sorted(p.stem for p in TEMPLATES_ADWS.glob("adw_*.py"))
RESUMING = {"adw_build", "adw_build_test", "adw_build_review", "adw_document"}
COMMITTING = {"adw_plan_build", "adw_plan_build_test", "adw_plan_build_test_quality",
              "adw_simple_sdlc"}

# An ADW an engineer writes: the shared CLI setup plus an option of its own.
CUSTOM_ADW = '''
import argparse
from adw_modules import session

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="what to do")
    parser.add_argument("--depth", choices=["shallow", "deep"], default="shallow",
                        help="how far to look")
    session.add_cli_args(parser)
    parser.parse_args()
    raise SystemExit("the ADW ran instead of describing itself")
'''


def describe(script: Path, cwd: Path) -> dict:
    env = {**os.environ, "PYTHONPATH": str(TEMPLATES_ADWS), "PYTHONUTF8": "1"}
    done = subprocess.run([sys.executable, str(script), "--describe"], cwd=cwd, env=env,
                          capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise AssertionError(f"{script.name} --describe exited {done.returncode}:\n"
                             f"{done.stdout}{done.stderr}")
    return json.loads(done.stdout)


def option(described: dict, flag: str) -> dict:
    found = [o for o in described["options"] if o["flag"] == flag]
    if not found:
        raise AssertionError(f"no {flag} in {[o['flag'] for o in described['options']]}")
    return found[0]


class DescribeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cwd = Path(cls.tmp.name)
        cls.described = {adw: describe(TEMPLATES_ADWS / f"{adw}.py", cls.cwd) for adw in ADWS}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_template_adw_describes_itself_without_starting_anything(self):
        self.assertGreater(len(ADWS), 10)
        self.assertEqual(sorted(self.described), ADWS)
        # No config read, no session dir, no trace db, no git: nothing was written.
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_every_adw_lists_its_prompt_and_the_shared_options(self):
        for adw, d in self.described.items():
            with self.subTest(adw=adw):
                prompt = [o for o in d["options"] if o["name"] == "prompt"]
                self.assertEqual(len(prompt), 1)
                self.assertIsNone(prompt[0]["flag"])
                self.assertTrue(prompt[0]["required"])
                config = option(d, "--config")
                self.assertEqual(config["kind"], "value")
                self.assertEqual(config["default"], "adws/adw_sssf_config/sssf.config.yaml")
                self.assertTrue(option(d, "--adw-id")["help"])
                flags = [o["flag"] for o in d["options"]]
                self.assertNotIn("--describe", flags)
                self.assertNotIn("--help", flags)

    def test_committing_adws_say_so_and_list_their_landing_options(self):
        for adw, d in self.described.items():
            with self.subTest(adw=adw):
                self.assertIs(d["commits"], adw in COMMITTING)
                flags = {o["flag"] for o in d["options"]}
                if adw not in COMMITTING:
                    self.assertNotIn("--in-place", flags)
                    self.assertEqual(d["mutually_exclusive"], [])
                    continue
                for flag in ("--in-place", "--allow-dirty", "--branch", "--merge", "--pr",
                             "--force"):
                    self.assertEqual(option(d, flag)["kind"], "flag", flag)
                    self.assertIs(option(d, flag)["default"], False, flag)
                self.assertEqual(d["mutually_exclusive"], [["--branch", "--merge", "--pr"]])

    def test_every_resuming_template_adw_reports_itself_as_one(self):
        for adw, d in self.described.items():
            with self.subTest(adw=adw):
                self.assertIs(d["resumes"], adw in RESUMING)

    def test_adw_specific_options_appear_alongside_the_shared_ones(self):
        agent = option(self.described["adw_prompt"], "--agent")
        self.assertEqual((agent["kind"], agent["default"], agent["help"]),
                         ("value", "builder", "agent name from the config"))
        base = option(self.described["adw_document"], "--base")
        self.assertEqual((base["kind"], base["default"]), ("value", "main"))

    def test_an_adw_written_with_the_shared_cli_setup_gets_describe_for_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "adw_custom.py"
            script.write_text(CUSTOM_ADW, encoding="utf-8")
            d = describe(script, Path(tmp))
        depth = option(d, "--depth")
        self.assertEqual((depth["kind"], depth["choices"], depth["default"], depth["help"]),
                         ("choice", ["shallow", "deep"], "shallow", "how far to look"))
        self.assertIs(d["commits"], False)
        self.assertIs(d["resumes"], False)

    def test_an_adw_generated_by_make_adw_describes_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, str(MAKE_ADW), "--name", "recon",
                            "--agents", "scout"], cwd=tmp, check=True, capture_output=True)
            script = Path(tmp) / "adws" / "adw_recon.py"
            d = describe(script, Path(tmp))
        self.assertEqual([o["flag"] for o in d["options"]], [None, "--config", "--adw-id"])


if __name__ == "__main__":
    unittest.main()
