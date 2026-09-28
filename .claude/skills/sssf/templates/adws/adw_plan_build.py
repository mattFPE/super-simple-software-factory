#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Plan Build — two-agent chain: planner -> envelope -> builder.

Usage:
    uv run adws/adw_plan_build.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
        [--merge | --pr] [--in-place [--allow-dirty]]

Runs in its own worktree on branch sssf/<adw_id> (see adw_modules/worktree.py);
your checkout is never touched. --in-place works in your checkout instead.

Phases: engineer(request) -> planner -> builder -> git(commit)
"""

import argparse
import sys

from adw_modules import agents, gates, git_helper, session, utils, worktree
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput, RunOptions

REQUIRED_AGENTS = ["planner", "builder"]


def main(prompt: str, opts: RunOptions) -> int:
    cfg = agents.load_config(opts.config)
    agents.validate(cfg, REQUIRED_AGENTS)
    worktree.preflight(opts)              # this run ends in a commit
    run = session.ensure(cfg, opts.adw_id)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    worktree.enter(run, opts)             # no-op with --in-place

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan exactly")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                  gates=[gates.diff_matches_claims]))

    with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                               description="Land the builder's changes, using the message it wrote")) as ph:
        message = build.commit_message or f"sssf({run.adw_id}): {build.summary}"
        ph.log(sha=git_helper.commit_all(message, run.repo_root), message=message)

    worktree.land(run, opts)              # branch / merge / PR; no-op in place
    return run.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    session.add_cli_args(parser, commits=True)
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), session.cli_options(args)))
