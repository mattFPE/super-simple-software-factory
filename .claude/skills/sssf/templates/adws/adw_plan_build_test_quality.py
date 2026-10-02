#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build Test Quality — full agent chain plus deterministic quality.

Usage:
    uv run adws/adw_plan_build_test_quality.py "<prompt, path/to/prompt.md, or #42>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
        [--branch | --merge | --pr] [--in-place [--allow-dirty]] [--force]

Runs in its own worktree on branch sssf/<adw_id> (see adw_modules/worktree.py);
your checkout is never touched. --in-place works in your checkout instead.

An issue as the prompt (`"#42"` or its URL; see adw_modules/issues.py) must be
labelled ready-for-agent and unblocked. The run claims it, lands as a PR that
closes it, and comments on it with the outcome.

Phases: engineer(request) -> planner -> builder -> [code(verify) -> code(test) -> builder(fix)] bounded -> git(commit)

Verify and test are CODE, not agents. Their commands are known, so running them
needs no judgement — only repairing them does. A failing block does not fail its
phase: the runner did its job, the code is what failed. The failure becomes an
envelope and flows back into the builder, and only an exhausted repair loop
fails the run.
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
                                     gates=[gates.diff_matches_claims,
                                            gates.tests_fail_without_change]))

    def record(ph, result) -> None:
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    test_result = None
    quality_result = None
    for i in range(1, MAX_FIX_LOOPS + 1):
        with run.phase(PhaseParams(name=f"verify_{i}", kind="code", owner="quality",
                                   description="Lint, typecheck, and build before testing")) as ph:
            quality_result = quality.run_quality(run)
            record(ph, quality_result)

        # run_quality() already includes the test block; a repo that wants tests
        # in their own phase can split them out the way this comment does.
        test_result = quality_result

        if quality_result.passed and test_result.passed:
            break
        if i == MAX_FIX_LOOPS:
            break

        # Whichever block failed becomes the builder's spec — verbatim command
        # output, no parser standing between the failure and the fix.
        broken = quality_result if not quality_result.passed else test_result
        what = "verification" if not quality_result.passed else "tests"
        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description=f"Resolve the reported {what} failures")) as ph:
            previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                         previous=quality.as_envelope(broken, what),
                                         gates=[gates.diff_matches_claims]))

    verified = (quality_result is not None and quality_result.passed
                and test_result is not None and test_result.passed)
    if verified:
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Commit the tested and quality-verified working tree")) as ph:
            message = previous.commit_message or f"sssf({run.adw_id}): {previous.summary}"
            ph.log(sha=git_helper.commit_all(message, run.repo_root), message=message)

    accepted = verified
    if accepted:                          # never merge or PR work that was not accepted;
        worktree.land(run, opts)          # a rejected run keeps its worktree to inspect
    return run.finish(accepted=accepted,
                      reason=f"verify/test never came back clean after {MAX_FIX_LOOPS} fix attempt(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text, a path to a prompt file, or an issue "
                        "(\"#42\", its URL, or a local issue's .scratch/ path)")
    session.add_cli_args(parser, commits=True)
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), session.cli_options(args)))
