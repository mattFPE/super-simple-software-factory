"""Deterministic lint, typecheck, build, and test blocks.

A known command is not a judgement call. Anything whose invocation you can write
down belongs here as code — it runs in milliseconds, costs nothing, and returns
the same answer every time. Agents are for the parts that need reading and
deciding.

The commands themselves live in `sssf.config.yaml` under `quality:`, as argv
lists — configuring a repo is a config edit, not a code edit:

    quality:
      test: [uv, run, pytest, -q]
      lint: [uv, run, ruff, check, .]

An unset command is NOT CONFIGURED, and that is never a pass: an ADW that needs
one declares it in REQUIRED_QUALITY and fails `agents.validate()` before any
agent spawns. (The factory used to ship `echo` placeholders that exited 0, so a
fresh install reported a green test phase for a suite that never ran.)

Two rules for the commands:
  1. argv LIST, never a shell string — no quoting bugs, no shell injection.
  2. Binaries by BARE NAME. They resolve on the operator's own PATH (see
     utils.operator_env), including Windows `.cmd` shims such as `npm`.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .data_types import (EventRecord, QualityCheckResult, QualityCheckSpec, QualityResult,
                         VerifyOutput)
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000


BLOCKS = ("test", "lint", "typecheck", "build")


def configured(cfg) -> list[str]:
    """The quality blocks this repo has a command for, in run order."""
    return [name for name in BLOCKS if getattr(cfg.quality, name)]


def resolve_argv(argv: list[str]) -> list[str]:
    """Resolve argv[0] on the operator's PATH (PATHEXT too, so `npm` finds
    `npm.cmd` on Windows, which CreateProcess alone would not)."""
    found = shutil.which(argv[0], path=operator_env().get("PATH"))
    return [found, *argv[1:]] if found else list(argv)


def preflight(cfg, required: list[str]) -> list[str]:
    """Problems that would stop a quality block before it runs."""
    problems = []
    for name in required:
        argv = getattr(cfg.quality, name, None)
        if not argv:
            problems.append(
                f"quality.{name} is not set in sssf.config.yaml — this ADW runs it. "
                f"Add e.g. `quality: {{{name}: [uv, run, pytest, -q]}}`")
        elif not shutil.which(argv[0], path=operator_env().get("PATH")):
            problems.append(f"quality.{name}: {argv[0]!r} not found on PATH")
    return problems


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1]
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    command = shlex.join(spec.argv)
    env = operator_env()             # the engineer's own shell environment

    run.console.note(f"quality {spec.name}: {command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            resolve_argv(spec.argv),
            cwd=run.repo_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = error.stdout or ""
        stderr = (error.stderr or "") + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        # A missing binary lands here as exit 127 with the real message — no
        # pre-flight probe needed, and none wanted.
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
        encoding="utf-8",
    )
    passed = returncode == 0
    run.tracer.event(EventRecord(
        adw_id=run.adw_id,
        phase_id=phase.phase_id,
        type="tool_call",
        name=f"quality:{spec.name}",
        payload={
            "area": spec.area,
            "operation": spec.operation,
            "command": command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
        },
        started_at=started_at,
        ended_at=now_iso(),
    ))
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        area=spec.area,
        operation=spec.operation,
        command=command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


# ── Blocks ────────────────────────────────────────────────────────────────────
# One per `quality:` key. The command comes from config; see the module docstring.

def _block(run, name: str) -> QualityCheckResult:
    argv = getattr(run.cfg.quality, name)
    if not argv:
        # validate() should have caught this; a direct caller gets the same answer.
        raise RuntimeError(f"quality.{name} is not configured in sssf.config.yaml")
    return _run(QualityCheckSpec(name=name, area="backend", operation=name, argv=argv,
                                 timeout_seconds=run.cfg.quality.timeout_seconds), run)


def test(run) -> QualityCheckResult:
    """Run the project's test suite. The highest-value block to configure first."""
    return _block(run, "test")


def lint(run) -> QualityCheckResult:
    return _block(run, "lint")


def typecheck(run) -> QualityCheckResult:
    return _block(run, "typecheck")


def build(run) -> QualityCheckResult:
    return _block(run, "build")


def run_tests(run) -> QualityResult:
    """The test suite alone, as a QualityResult — the deterministic test phase.

    This is what replaces a `tester` agent once the command is written down. An
    agent rediscovering the runner on every run costs a fortune to learn what a
    subprocess already knows; the repair loop is unchanged, because a failure
    still reaches the builder through `as_envelope` below.
    """
    check = test(run)
    failures = ([] if check.passed else
                [f"{check.name}: `{check.command}` exited {check.returncode}\n"
                 f"{check.output_tail}".rstrip()])
    return QualityResult(passed=check.passed, checks=[check], failures=failures,
                         artifacts=[check.output_artifact])


def as_envelope(result: QualityResult, what: str) -> VerifyOutput:
    """Wrap a deterministic result so an agent can be handed it directly.

    Agents hand each other typed envelopes; code blocks return QualityResult.
    This is the adapter, so a failing lint or test run flows back into the
    builder through exactly the same door an agent's report would — the ADW
    script is the only thing that knows the difference.
    """
    return VerifyOutput(
        status="success" if result.passed else "fail",
        summary=(f"{what}: all {len(result.checks)} check(s) passed" if result.passed
                 else f"{what}: {len(result.failures)} of {len(result.checks)} check(s) failed"),
        artifacts=result.artifacts,
        notes_for_next_agent=("" if result.passed else
                              "Fix every failure below. The output is verbatim from the "
                              "command — trust it over any summary."),
        passed=result.passed,
        failures=result.failures,
    )


def run_quality(run) -> QualityResult:
    """Run every block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.
    """
    blocks: dict[str, Callable] = {"test": test, "lint": lint,
                                   "typecheck": typecheck, "build": build}
    names = configured(run.cfg)
    for name in BLOCKS:
        if name not in names:
            run.console.note(f"quality {name}: not configured — skipped")
    checks = [blocks[name](run) for name in names]
    if not checks:
        raise RuntimeError("no quality commands are configured in sssf.config.yaml")
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
