"""Installing and updating leave a working Console, whatever the repo had before (#7).

A throwaway target repo with the skill copied in, as the README's quick start
does, stamped by the installer. The repos "from before the rename" are built
from the last justfile in git history that still pointed at apps/visualizer,
and a skill copy laid out as it was then. Each case ends by running the
recipes for real: `console-status`, and the `obs*` aliases kept for one release.

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".claude" / "skills" / "sssf"
COPY = Path(".claude") / "skills" / "sssf"
JUSTFILE = "templates/justfile"
SKIP = {"node_modules", "dist", "__pycache__", ".git"}


def run(argv: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def git(cwd: Path, *args: str) -> str:
    done = run(["git", *args], cwd)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout


def pre_rename_justfile() -> str:
    """The newest committed template justfile that still ran the visualizer."""
    for commit in git(SKILL, "log", "--format=%H", "--", JUSTFILE).split():
        text = git(SKILL, "show", f"{commit}:./{JUSTFILE}")
        if "apps/visualizer" in text:
            return text
    raise AssertionError("no pre-rename justfile in history")


def _writable(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


@unittest.skipUnless(shutil.which("just") and shutil.which("bun") and shutil.which("git"),
                     "needs just, bun and git")
class ConsoleAcrossTheRename(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="sssf-console-rename-"))
        self.addCleanup(shutil.rmtree, self.repo, onerror=_writable)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        # What a repo that has run its UI ignores; the stamp does not add these.
        (self.repo / ".gitignore").write_text("node_modules/\ndist/\n", encoding="utf-8")
        shutil.copytree(SKILL, self.repo / COPY, ignore=lambda _d, names: SKIP & set(names))
        self.port = str(random.randint(20000, 30000))

    def install(self, *args: str) -> str:
        done = run([sys.executable, str(SKILL / "scripts" / "install.py"), *args], self.repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def commit(self) -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "state")

    def make_pre_rename(self, justfile: str) -> None:
        """The repo as an install from before the rename left it."""
        (self.repo / "justfile").write_text(justfile, encoding="utf-8")
        apps = self.repo / COPY / "apps"
        apps.joinpath("console").rename(apps / "visualizer")
        server = apps / "visualizer" / "server"
        server.joinpath("background.ts").rename(server / "obs.ts")
        # What running the old UI left behind, which no update tracks.
        for leftover in ("node_modules/vue/index.js", "dist/index.html"):
            path = apps / "visualizer" / leftover
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def just(self, recipe: str) -> subprocess.CompletedProcess:
        return run(["just", recipe], self.repo, env={**os.environ, "PORT": self.port})

    def assert_console_works(self) -> None:
        for recipe in ("console-status", "obs-status", "console-stop", "obs-stop"):
            done = self.just(recipe)
            self.assertEqual(done.returncode, 0, f"just {recipe}: {done.stdout}{done.stderr}")
            self.assertIn(f"Console is not running on :{self.port}", done.stdout,
                          f"just {recipe}")

    def test_a_fresh_install_runs_the_console(self) -> None:
        out = self.install()
        self.assertIn("just console", out)
        self.assertNotIn("visualizer", (self.repo / "justfile").read_text(encoding="utf-8"))
        self.assert_console_works()

    def test_an_update_moves_an_unedited_install_onto_the_console(self) -> None:
        self.install()
        self.make_pre_rename(pre_rename_justfile())
        self.commit()
        self.install("--update")
        justfile = (self.repo / "justfile").read_text(encoding="utf-8")
        self.assertTrue("apps/console" in justfile and "apps/visualizer" not in justfile,
                        "the justfile still runs the visualizer")
        self.assertFalse((self.repo / COPY / "apps" / "visualizer").exists())
        self.assertTrue((self.repo / COPY / "apps" / "console" / "server" / "background.ts").is_file())
        self.assert_console_works()

    def test_an_update_points_an_edited_justfile_at_the_console(self) -> None:
        self.install()
        mine = "\n# my own recipe\nhello:\n    @echo hello from me\n"
        self.make_pre_rename(pre_rename_justfile() + mine)
        self.commit()
        out = self.install("--update")
        justfile = (self.repo / "justfile").read_text(encoding="utf-8")
        self.assertIn(mine.strip(), justfile)
        self.assertFalse("apps/visualizer" in justfile or "server/obs.ts" in justfile,
                         "the edited justfile still runs the visualizer")
        self.assertIn("justfile:", out)
        self.assertIn("hello from me", self.just("hello").stdout)
        self.assert_console_works()

    def test_an_update_gives_a_justfile_without_ui_recipes_the_console_and_aliases(self) -> None:
        self.install()
        old = pre_rename_justfile()
        self.make_pre_rename(old[:old.index("# ── observability UI")])
        self.commit()
        self.install("--update")
        justfile = (self.repo / "justfile").read_text(encoding="utf-8")
        self.assertIn("alias obs := console", justfile)
        self.assert_console_works()
        self.install("--update", "--allow-dirty")
        self.assertEqual(justfile, (self.repo / "justfile").read_text(encoding="utf-8"),
                         "a second update added the recipes again")


if __name__ == "__main__":
    unittest.main()
