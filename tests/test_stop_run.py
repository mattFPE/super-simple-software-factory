"""Stopping a Run from its recorded processes, as `just kill` and the Console do (#10).

`adw_modules/procs.py stop <adw_id>` runs for real against a Run that is
really running: a small ADW built on the shared session module, blocked in an
agent phase on a fake agent that never answers. Stop kills the agent before
the ADW, checks every pid against its recorded command first, and the Run
settles itself as stopped, not failed.

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
TEMPLATES_ADWS = SKILL / "templates" / "adws"
PROCS = TEMPLATES_ADWS / "adw_modules" / "procs.py"
ADW_ID = "5709c0de"

sys.path.insert(0, str(TEMPLATES_ADWS))
from adw_modules.procs import pid_alive  # noqa: E402

# An ADW blocked in an agent phase, the way the adapters block: reading the
# agent's output until it ends. The agent prints one line and then hangs.
HANGING_ADW = r'''
import pathlib, subprocess, sys
from adw_modules import procs, session
from adw_modules.data_types import PhaseParams, SSSFConfig

cfg = SSSFConfig.model_validate({"defaults": {"data_dir": "data"},
                                 "observability": {"db": "data/sssf.db"}})
run = session.ensure(cfg, sys.argv[1])
run.when_settled(lambda ok: pathlib.Path("settled").write_text(
    f"ok={ok} stopped={run.stopped}", encoding="utf-8"))
with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                           description="Wait on an agent that never answers, to be stopped")):
    agent = subprocess.Popen([sys.executable, "-c",
                              "import time; print('ready', flush=True); time.sleep(300)",
                              "fake-agent"],
                             stdout=subprocess.PIPE, text=True, **procs.popen_kwargs())
    run.tracer.process_start(run.adw_id, "agent", "builder", agent.pid,
                             procs.recorded_command(agent.args))
    with procs.supervise(agent, 0, "fake agent"):
        for line in agent.stdout:
            pathlib.Path("agent.pid").write_text(str(agent.pid), encoding="utf-8")
    raise SystemExit("the agent ended without anyone stopping it")
sys.exit(run.finish())
'''


def env() -> dict:
    return {**os.environ, "PYTHONPATH": str(TEMPLATES_ADWS), "PYTHONUTF8": "1",
            "ENGINEER_NAME": "test"}


def wait_for(done, seconds: float = 30) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if done():
            return True
        time.sleep(0.1)
    return False


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.db = self.cwd / "data" / "sssf.db"
        self.spawned: list[subprocess.Popen] = []

    def tearDown(self):
        for proc in self.spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.tmp.cleanup()

    def spawn(self, argv: list[str], **kwargs) -> subprocess.Popen:
        proc = subprocess.Popen(argv, cwd=self.cwd, env=env(), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kwargs)
        self.spawned.append(proc)
        return proc

    def stop(self, adw_id: str, *flags: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(PROCS), "stop", adw_id, "--db", str(self.db),
                               *flags], cwd=self.cwd, env=env(), capture_output=True,
                              text=True, encoding="utf-8", timeout=120)

    def query(self, sql: str, *args) -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, args).fetchall()

    def start_hanging_run(self) -> tuple[subprocess.Popen, int]:
        (self.cwd / "adw_hang.py").write_text(HANGING_ADW, encoding="utf-8")
        adw = self.spawn([sys.executable, "adw_hang.py", ADW_ID])
        pid_file = self.cwd / "agent.pid"
        self.assertTrue(wait_for(pid_file.exists), "the stub ADW never reached its agent")
        return adw, int(pid_file.read_text(encoding="utf-8"))

    def test_stop_kills_the_agent_then_the_adw_and_the_run_settles_as_stopped(self):
        adw, agent_pid = self.start_hanging_run()

        done = self.stop(ADW_ID, "--json")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual(report["status"], "stopped")
        self.assertEqual([(p["kind"], p["outcome"]) for p in report["processes"]],
                         [("agent", "killed"), ("adw", "stopped")])
        self.assertEqual(adw.wait(timeout=30) != 0, True)
        self.assertFalse(pid_alive(agent_pid))
        self.assertEqual(self.query("SELECT status FROM sessions WHERE adw_id=?", ADW_ID),
                         [("stopped",)])
        self.assertEqual(self.query("SELECT name, status FROM phases WHERE adw_id=?", ADW_ID),
                         [("build", "fail")])
        self.assertEqual(self.query("SELECT COUNT(*) FROM processes WHERE adw_id=? "
                                    "AND ended_at IS NULL", ADW_ID), [(0,)])
        # The run settled itself: an issue's claim is released by this same hook.
        self.assertEqual((self.cwd / "settled").read_text(encoding="utf-8"),
                         "ok=False stopped=True")

    def test_a_pid_whose_command_no_longer_matches_is_not_signalled(self):
        stranger = self.spawn([sys.executable, "-c", "import time; time.sleep(300)"])
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db, isolation_level=None)) as conn:
            from adw_modules.tracer import SCHEMA
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO sessions (adw_id, status, started_at) "
                         "VALUES (?, 'running', '2026-01-01T00:00:00.000+00:00')", (ADW_ID,))
            conn.execute("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) "
                         "VALUES (?, 'adw', '', ?, 'adw_build.py --adw-id 5709c0de -- fix it', "
                         "'2026-01-01T00:00:00.000+00:00')", (ADW_ID, stranger.pid))

        done = self.stop(ADW_ID, "--json")

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        report = json.loads(done.stdout)
        [process] = report["processes"]
        self.assertEqual(process["outcome"], "mismatch")
        self.assertEqual(process["pid"], stranger.pid)
        self.assertTrue(pid_alive(stranger.pid))
        self.assertIsNone(stranger.poll())
        # Its own process is long gone, so the Run is settled for it.
        self.assertEqual(report["status"], "stopped")
        self.assertEqual(self.query("SELECT status FROM sessions WHERE adw_id=?", ADW_ID),
                         [("stopped",)])

    def test_a_pid_now_running_another_runs_agent_is_not_signalled(self):
        # Same program, same first arguments: only the session dir in its argv differs.
        sleep = "import time; time.sleep(300)"
        stranger = self.spawn([sys.executable, "-c", sleep, "data/sessions/0therrun/builder"])
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db, isolation_level=None)) as conn:
            from adw_modules.tracer import SCHEMA
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO sessions (adw_id, status, started_at) "
                         "VALUES (?, 'running', '2026-01-01T00:00:00.000+00:00')", (ADW_ID,))
            conn.execute("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) "
                         "VALUES (?, 'agent', 'builder', ?, ?, '2026-01-01T00:00:00.000+00:00')",
                         (ADW_ID, stranger.pid,
                          f"{sys.executable} -c {sleep} data/sessions/{ADW_ID}/builder"))

        done = self.stop(ADW_ID, "--json")

        [process] = json.loads(done.stdout)["processes"]
        self.assertEqual(process["outcome"], "mismatch")
        self.assertIsNone(stranger.poll())

    def test_from_the_terminal_it_says_what_it_did(self):
        self.start_hanging_run()

        done = self.stop(ADW_ID)

        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("killed agent builder", done.stdout)
        self.assertIn(f"{ADW_ID} stopped", done.stdout)

    def test_a_run_that_is_not_running_is_refused(self):
        adw, _ = self.start_hanging_run()
        self.stop(ADW_ID)
        adw.wait(timeout=30)

        again = self.stop(ADW_ID, "--json")
        unknown = self.stop("nosuchrun", "--json")

        self.assertEqual(again.returncode, 2)
        self.assertIn("not running", json.loads(again.stdout)["error"])
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("no Run", json.loads(unknown.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
