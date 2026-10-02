"""Session lifecycle: pin-or-create an adw_id, build the Run object.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.
"""

from __future__ import annotations

import _thread
import argparse
import atexit
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import issues, procs
from .data_types import RunOptions, SSSFConfig
from .runner import Run
from .tracer import Tracer
from .utils import engineer_name, new_id


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so `just kill`
    (or any `kill <pid>`) would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.
    The Run settles as `stopped`, not `fail`: nothing broke, someone ended it.
    """
    def handler(signum, _frame):
        if run.stopped:
            return                        # already on the way out: a second Ctrl+C, say
        run.stopped = True
        run.tracer.session_finish(run.adw_id, ok=False, stopped=True)   # also closes process rows
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)
    _stop_when_requested(run)

    def on_exit() -> None:
        # Anything that ends the interpreter without run.finish() — an exception
        # between phases, a bug in the ADW script — still closes the trace.
        if not run.finalized:
            run.tracer.session_finish(run.adw_id, ok=False, stopped=run.stopped)
            run.settle(ok=False)          # e.g. an issue's claim is still released
    atexit.register(on_exit)


def _stop_when_requested(run: Run) -> None:
    """Treat a stop request in the session dir as SIGTERM (`procs.stop`).

    On Windows a signal can't reach a detached process without killing it, so
    this is how the Console's Stop and `just kill` reach the Run there. Removing
    the file is the acknowledgement `procs.stop` waits for before it kills the
    agents. The handler runs as soon as the main thread is back in Python,
    which a killed agent's closed output makes it.
    """
    request = run.session_dir / procs.STOP_REQUEST
    request.unlink(missing_ok=True)       # an unheard request left for an earlier ADW

    def watch() -> None:
        while not request.exists():
            time.sleep(0.25)
        request.unlink(missing_ok=True)
        _thread.interrupt_main(signal.SIGTERM)

    threading.Thread(target=watch, name="sssf-stop-request", daemon=True).start()


def ensure(cfg: SSSFConfig, adw_id: str | None = None) -> Run:
    adw_id = adw_id or new_id(8)
    tracer = Tracer(cfg.observability.db,
                    f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl")
    # Runs that died without closing their trace (hard kill, crash, reboot)
    # would read `running` forever; every new run sweeps them first.
    abandoned = tracer.reap_abandoned(procs.pid_alive)
    run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer=engineer_name())
    tracer.session_start(adw_id, run.engineer, adw_name=Path(sys.argv[0]).stem)
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by adw_id.
    tracer.process_start(adw_id, "adw", "", os.getpid(),
                         " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
    _finalize_when_killed(run)
    run.console.session_started(adw_id, run.engineer)
    if abandoned:
        run.console.note(f"closed {len(abandoned)} abandoned run(s) as failed: "
                         + ", ".join(abandoned))
    return run


# ── CLI ──────────────────────────────────────────────────────────────────────

class _Describe(argparse.Action):
    """`--describe`: print the ADW's command line as JSON and exit, starting nothing.

    It runs while argv is parsed, like `--help`, so the ADW's own code — config,
    session, worktree, agents — is never reached, and the prompt isn't required.
    """

    def __init__(self, option_strings, dest, commits: bool, resumes: bool, **kwargs):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs)
        self.commits, self.resumes = commits, resumes

    def __call__(self, parser, namespace, values, option_string=None):
        # stdout, not run.console: no Run exists yet, and the Console parses this as JSON.
        print(json.dumps(describe(parser, self.commits, self.resumes), indent=2, default=str))
        parser.exit()


def _flag(action) -> str | None:
    """The option's long spelling (`--adw-id`); None for a positional."""
    strings = action.option_strings
    return next((s for s in strings if s.startswith("--")), strings[0] if strings else None)


def describe(parser, commits: bool, resumes: bool) -> dict:
    """The options `parser` accepts, read from the parser itself so it can't drift."""
    shown = [a for a in parser._actions
             if not isinstance(a, (argparse._HelpAction, _Describe))
             and a.help is not argparse.SUPPRESS]               # hidden on purpose
    return {
        "commits": commits,
        "resumes": resumes,
        "options": [{
            "name": a.dest,
            "flag": _flag(a),
            "kind": "flag" if a.nargs == 0 else "choice" if a.choices else "value",
            "help": a.help,
            "default": a.default,
            "choices": list(a.choices) if a.choices else None,
            "required": a.required,
        } for a in shown],
        "mutually_exclusive": [[_flag(a) for a in group._group_actions]
                               for group in parser._mutually_exclusive_groups],
    }


def add_cli_args(parser, commits: bool = False, resumes: bool = False) -> None:
    """The flags every ADW shares; `commits=True` adds the worktree/landing ones.

    `resumes=True` marks a Resuming ADW: one that continues an earlier Run's work
    under its `--adw-id`. Both are reported by `--describe`, which every ADW gets.
    """
    parser.add_argument("--describe", action=_Describe, commits=commits, resumes=resumes,
                        help="print this ADW's options as JSON and exit")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    if not commits:
        return
    parser.add_argument("--in-place", action="store_true",
                        help="work and commit in your checkout instead of a worktree "
                             "(needs a clean tree)")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="with --in-place: start anyway; your changes land in the commit")
    land = parser.add_mutually_exclusive_group()
    land.add_argument("--branch", action="store_true",
                      help="after the commit, leave the run's branch for you to take "
                           "(the default, except for an issue, which defaults to --pr)")
    land.add_argument("--merge", action="store_true",
                      help="after the commit, merge the run's branch into the one you started from")
    land.add_argument("--pr", action="store_true",
                      help="after the commit, push the run's branch and open a PR with gh")
    parser.add_argument("--force", action="store_true",
                        help="an issue: run it even though it is labelled as running or an "
                             "open PR already closes it (e.g. after a hard kill)")


def cli_options(args) -> RunOptions:
    commits = hasattr(args, "merge")
    # The prompt was already resolved (utils.resolve_prompt), so this is a cache hit.
    issue = issues.load(args.prompt) if issues.parse_ref(args.prompt) else None
    if issue and commits:
        issues.require_runnable(issue, force=args.force)
    in_place = getattr(args, "in_place", False)
    opts = RunOptions(
        config=args.config, adw_id=args.adw_id, issue=issue,
        in_place=in_place,
        allow_dirty=getattr(args, "allow_dirty", False),
        land=("merge" if getattr(args, "merge", False)
              else "pr" if getattr(args, "pr", False)
              else "branch" if getattr(args, "branch", False)
              # An issue ends where it can be closed: a PR. In place there is
              # no branch of the run's own to open one from.
              else "pr" if issue and commits and not in_place else "branch"))
    if opts.allow_dirty and not opts.in_place:
        raise SystemExit("--allow-dirty only applies with --in-place: a worktree run starts "
                         "from your last commit and never sees your uncommitted changes")
    if opts.in_place and opts.land != "branch":
        raise SystemExit("--merge / --pr end a worktree run; with --in-place the commit is "
                         "already on your branch")
    if getattr(args, "force", False) and not issue:
        raise SystemExit("--force only applies when the prompt is an issue (#42 or its URL)")
    return opts
