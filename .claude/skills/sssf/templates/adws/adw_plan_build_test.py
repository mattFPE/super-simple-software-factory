#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build Test — the full starter chain.

Usage:
    uv run adws/adw_plan_build_test.py "<prompt, path/to/prompt.md, or #42>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
        [--branch | --merge | --pr] [--in-place [--allow-dirty]] [--force]

Runs in its own worktree on branch sssf/<adw_id> (see adw_modules/worktree.py);
your checkout is never touched. --in-place works in your checkout instead.

An issue as the prompt (`"#42"` or its URL; see adw_modules/issues.py) must be
labelled ready-for-agent and unblocked. The run claims it, lands as a PR that
closes it, and comments on it with the outcome.

Phases: engineer(request) -> planner -> builder -> code(test) [-> builder(fix) -> code(test) ... bounded] -> git(commit)

Testing is CODE: the suite's command lives in adw_modules/quality.py, so no
agent spends a context window rediscovering it. Failures flow back to the
builder as an envelope, and only an exhausted fix loop fails the run.
"""

import argparse
import sys

from adw_modules import agents, gates, git_helper, issues, quality, session, utils, worktree
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput, RunOptions

REQUIRED_AGENTS = ["planner", "builder"]
REQUIRED_QUALITY = ["test"]           # validate() refuses to start without it
MAX_FIX_LOOPS = 3


def main(prompt: str, opts: RunOptions) -> int:
    cfg = agents.load_config(opts.config)
    agents.validate(cfg, REQUIRED_AGENTS, REQUIRED_QUALITY)
    worktree.preflight(opts)              # this run ends in a commit
    run = session.ensure(cfg, opts.adw_id)

    def record(ph, result) -> None:
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    issues.claim(run, opts)               # no-op unless the request is an issue
    worktree.enter(run, opts)             # no-op with --in-place

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                               description="Implement the plan exactly, test-first")) as ph:
        previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                     gates=[gates.artifacts_exist,
                                            gates.tests_fail_without_change]))

    test = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run the suite — a known command, so code runs "
                                               "it and no agent has to rediscover it")) as ph:
            test = quality.run_tests(run)
            record(ph, test)

        if test.passed:
            break

        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description="Repair what the suite reported, from its "
                                               "verbatim output")) as ph:
            previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                         previous=quality.as_envelope(test, "tests"),
                                         gates=[gates.artifacts_exist]))

    # Only tested work gets committed — a red suite leaves the tree uncommitted.
    if test is not None and test.passed:
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Land the code only after the suite came back green")) as ph:
            message = previous.commit_message or f"sssf({run.adw_id}): {previous.summary}"
            ph.log(sha=git_helper.commit_all(message, run.repo_root), message=message)

    accepted = test is not None and test.passed
    if accepted:                          # never merge or PR work that was not accepted;
        worktree.land(run, opts)          # a rejected run keeps its worktree to inspect
    return run.finish(accepted=accepted,
                      reason=f"the suite still failed after {MAX_FIX_LOOPS} fix attempt(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text, a path to a prompt file, or a GitHub "
                        "issue (\"#42\" or its URL)")
    session.add_cli_args(parser, commits=True)
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), session.cli_options(args)))
