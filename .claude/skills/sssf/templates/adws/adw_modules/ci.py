"""Wait for a run's pull request to go green: the repo's CI, as a phase.

The gates and quality blocks judge a run on this machine. The repo's CI judges
the PR, often on more (other OSes, linters the local config doesn't run, e2e),
and it reports minutes after the run has already said ✅. With
`pr.wait_for_checks` on, a `--pr` run waits for that verdict and takes it as
its own: a red check fails the run, and the issue comment names the checks.

The PR is left as it is either way. By now the branch is pushed and the
worktree is gone, so a red PR is fixed on the PR, or by a fresh run.
"""

from __future__ import annotations

import json
import subprocess
import time

from .data_types import PhaseParams
from .utils import operator_env

POLL_SECONDS = 15
APPEAR_SECONDS = 120        # how long checks get to show up before "this repo has none"
READ_ATTEMPTS = 3           # consecutive failed reads before a network blip counts
FAILED = {"fail", "cancel"}
TAIL_CHARS = 1500


def wait(run) -> None:
    """The `checks` phase: after `land` opened a PR, wait for its checks to settle.

    Does nothing unless the run opened a PR and `pr.wait_for_checks` is on.
    """
    pr = run.landed.get("pr")
    if not (pr and run.cfg.pr.wait_for_checks):
        return
    with run.phase(PhaseParams(
            name="checks", kind="code", owner="github",
            description="Wait for the pull request's CI checks, and take their verdict "
                        "as the run's")) as ph:
        checks = _settled(run, pr)
        failed = [c for c in checks if c.get("bucket") in FAILED]
        run.report["checks"] = _summary(checks, failed)
        ph.log(pr=pr, checks=len(checks), failed=len(failed))
        if failed:
            names = ", ".join(c.get("name", "?") for c in failed)
            raise RuntimeError(f"{len(failed)} of {len(checks)} CI check(s) failed on {pr}: "
                               f"{names}")


def _settled(run, pr: str) -> list[dict]:
    """Every check on the PR once none is pending; [] if none appear at all."""
    timeout = run.cfg.pr.checks_timeout_seconds
    started = time.monotonic()
    last = ""
    while True:
        checks = _read(pr)
        waited = time.monotonic() - started
        pending = [c for c in checks if c.get("bucket") == "pending"]
        if checks and not pending:
            return checks
        if not checks and waited >= min(APPEAR_SECONDS, timeout):
            run.console.note(f"no checks reported on the PR within {int(waited)}s — "
                             "nothing to wait for")
            return []
        if waited >= timeout:
            names = ", ".join(c.get("name", "?") for c in pending)
            raise RuntimeError(f"{len(pending)} CI check(s) still running after {timeout}s: "
                               f"{names}. Raise pr.checks_timeout_seconds, or follow them "
                               f"on {pr}/checks")
        status = (f"{len(checks) - len(pending)} of {len(checks)} check(s) done" if checks
                  else "waiting for checks to start")
        if status != last:
            run.console.note(status)
            last = status
        time.sleep(POLL_SECONDS)


def _read(pr: str) -> list[dict]:
    """The PR's checks, as gh reports them. Retries a failed read a few times:
    one network blip must not fail a run that has been waiting half an hour."""
    for attempt in range(READ_ATTEMPTS):
        done = subprocess.run(
            ["gh", "pr", "checks", pr, "--json", "name,bucket,link,workflow"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=operator_env())
        out = done.stdout.strip()
        if out.startswith("["):           # JSON whatever the exit code (8 = pending)
            return json.loads(out)
        if "no checks reported" in done.stderr:
            return []
        if attempt < READ_ATTEMPTS - 1:
            time.sleep(POLL_SECONDS)
    raise RuntimeError(f"gh pr checks {pr} failed: {done.stderr.strip()[-TAIL_CHARS:]}")


def _summary(checks: list[dict], failed: list[dict]) -> str:
    """What the issue comment says about CI."""
    if not checks:
        return "**CI:** no checks reported on the pull request."
    if not failed:
        return f"**CI:** all {len(checks)} check(s) passed or were skipped."
    lines = [f"- ❌ [{c.get('name', '?')}]({c.get('link', '')})"
             + (f" — {c['workflow']}" if c.get("workflow") else "") for c in failed]
    return f"**CI:** {len(failed)} of {len(checks)} check(s) failed:\n" + "\n".join(lines)
