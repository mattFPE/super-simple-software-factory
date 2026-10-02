# Run ADW

Run a workflow and report on it. **You run and observe — you never step into the process or do the work yourself.**

## Step 0 — translate the request

**Read [how_to_prompt_for_the_eng.md](how_to_prompt_for_the_eng.md) before you launch anything.** The prompt you pass is read by every agent in the chain, so it gets written deliberately: same intent, sharper words, verified paths, and a stated "done means". That cookbook is the whole procedure; this one starts once you have the prompt.

## The orchestrator's posture

The ADW is the worker. Your job is to launch it, watch the trace, and tell the engineer what happened. Do not read the agent's target files and "help", do not fix the code an agent was supposed to fix, do not edit an envelope. If a run fails, report the failing phase and its violations — the fix is a config, prompt, or ADW change, made deliberately, and then a re-run.

## Launch

Which chain to launch is decided in `how_to_prompt_for_the_eng.md`, and the short version is: **the ADW the engineer named, or else the most complete composed chain the work justifies — never a single-agent one.** Read `ls adws/adw_*.py` and the `Phases:` line in each docstring to see what this repo has; the names below are shape, not a menu.

```bash
uv run adws/<end-to-end-chain>.py "add a /health endpoint"
uv run adws/<plan-build-verify-chain>.py requests/health.md
uv run adws/<build-first-chain>.py "implement the plan" --adw-id a1b2c3d4
uv run adws/<recon-chain>.py "where is auth handled" --config path/to/other.config.yaml
```

**A chain that commits runs in its own worktree** (`../<repo>.sssf-worktrees/<adw_id>`, branch `sssf/<adw_id>`) and never touches the engineer's checkout. It builds on their last commit, not on uncommitted edits, so say so if they have some. It ends as a branch unless they asked for `--merge` or `--pr`; `--in-place` is the old behaviour. After a run, report the branch and how to take it (`git merge sssf/<id>`), and on failure where the kept worktree is.

The prompt is inline text, a file path, or a GitHub issue (`"#42"` or its URL). Launch in the background so you can poll while it works; the `adw_id` is printed on startup — capture it, everything else keys off it.

**An issue runs as a ticket** (`adw_modules/issues.py`). It must carry the repo's ready-for-agent label (mapped in `docs/agents/triage-labels.md` when `/setup-matt-pocock-skills` wrote one), and a committing chain also refuses it when it is closed, has an open blocker, is a spec already split into tickets, or is labelled `agent-running` / has an open PR that closes it (`--force` overrides only those last two). The run labels it `agent-running` in a `claim` phase, defaults to `--pr` with `Closes #42` in the body, and when it ends removes the label and comments with the outcome, including on failure and Ctrl+C. After the run, report the PR link. On a failure, the issue comment already carries the rerun command.

**In a Local Markdown repo** (`docs/agents/issue-tracker.md` is headed `Local Markdown`), the issue is a path instead: `.scratch/<feature>/issues/NN-<slug>.md` for a Ticket, `.scratch/<feature>/spec.md` for a Spec, and `#42` is refused. The same rules apply, read from the file: `Status:` must be the ready label, `Blocked by:` numbers or titles name Tickets in the same folder and only `Status: resolved` clears one (a reference that names nothing still blocks), a Ticket runs with its `spec.md` attached, and a Spec with an `issues/` folder refuses, naming its Runnable Tickets. Comments under `## Comments` reach the agent, except sssf's own. The run claims it as `Status: agent-running` in the engineer's checkout, uncommitted, and defaults to `--merge`. A failure (or a stop) restores the ready Status and appends the outcome comment with the rerun command, uncommitted. A successful merge commits `Status: resolved` and the outcome on the run's branch before merging, so the Ticket resolves with its code; a successful `--in-place` run sets `Status: resolved`, uncommitted; a successful `--branch` or `--pr` sets `Status: ready-for-human` and names the branch, so its dependents wait for review. No agent may edit `.scratch/` (unless a `writes:` entry names it), code commits leave it out, and a pending Claim there never makes a run refuse a dirty tree.

### Listen for the roster

The chain says *what runs*; the config says *who runs it*. **If the engineer references a roster, a config, or a model tier, pass it — do not fall through to the default.**

```bash
just rosters                            # every roster on disk, and the model each agent runs
```

That prints the path to pass and who is in it, in one read:

```
adws/adw_sssf_config/sssf.config.yaml
    planner     fireworks/accounts/fireworks/models/kimi-k3
    builder     google/gemini-3.6-flash (inherited)
adws/adw_sssf_config/sssf.frontier.config.yaml
    planner     anthropic/claude-opus-5
```

Read those from disk every time. Rosters are the engineer's to add, rename, and retune, so a name you remember from a doc is a guess.

They will rarely say `--config`. Treat any of these as naming a roster, then resolve it to a file:

| What they say | What it means |
|---|---|
| "run it on the frontier config", "use the frontier roster" | the roster file whose name matches |
| "run this with the big models", "use the sota roster" | the non-default roster — confirm which if there is more than one. Each config's header comment lists the names it answers to, so `head -3` on the file settles it |
| "have opus plan this one" | a roster whose planner is that model; if none exists, say so rather than editing the config mid-request |
| nothing about models at all | the default, `adws/adw_sssf_config/sssf.config.yaml` |

`--config` takes the path directly; the justfile recipes read `SSSF_CONFIG` instead:

```bash
uv run adws/<chain>.py "<prompt>" --config adws/adw_sssf_config/sssf.frontier.config.yaml
SSSF_CONFIG=adws/adw_sssf_config/sssf.frontier.config.yaml just <recipe> "<prompt>"
```

Two things that bite:

- **Never swap rosters on your own.** A different roster is a different cost and a different result. If the default's model looks wrong for the work, say so and let the engineer choose.
- **Switching rosters mid-session breaks resumption.** `agent_map.json` records the model each coding-agent session was created with, so a joined run (`--adw-id`) whose config now names a different model starts that agent **fresh** instead of resuming its context window. That is deliberate — a bad resume is worse — but it means "plan on the frontier roster, then build on the default" costs the builder its accumulated context. Say so when you report it.

`--adw-id` is optional on **every** ADW. Given one, the run joins that session if it exists or creates it pinned to exactly that id: same `sessions/{adw_id}/` dirs, same `context_handoff/`, envelopes appended, and each agent resumes its existing coding-agent context window via `agent_map.json`. That is how you chain ADWs — plan under one id, then build under the same id.

## Observe

The trace db is `adws/adw_data/sssf.db`. It is WAL, so reads never block the running writers — poll it as often as you like.

```bash
# where the run stands
sqlite3 adws/adw_data/sssf.db \
  "select seq, name, kind, owner, status, attempt from phases where adw_id='a1b2c3d4' order by seq;"

# the live tail — cursor on rowid, same query the Console polls
sqlite3 adws/adw_data/sssf.db \
  "select rowid, type, name, started_at from events where adw_id='a1b2c3d4' and rowid > 0 order by rowid limit 50;"

# why a phase failed
sqlite3 adws/adw_data/sssf.db \
  "select attempt, gate, passed, checks_json from gate_results where adw_id='a1b2c3d4';"

# session-level status
sqlite3 adws/adw_data/sssf.db \
  "select adw_id, request, status, total_tokens from sessions order by started_at desc limit 5;"

# what an agent actually did, slowest tool calls first
sqlite3 adws/adw_data/sssf.db \
  "select name, tokens, started_at, ended_at from events
   where adw_id='a1b2c3d4' and type='tool_call' order by ended_at desc limit 20;"
```

Poll on a cursor: keep the highest `rowid` you have seen and query `where rowid > ?`. Don't re-read the whole table each pass.

`tool_call` rows carry a real span, so durations come off the columns — see `references/observability.md` for which fields each event type populates.

The ADW also narrates to stdout, and every line it prints is written to the db as a `log` event — terminal and swim lane tell the same story by construction, so tailing the background process is a valid second view rather than a competing source of truth.

Files are the raw record if you need more than the db shows: `adws/adw_data/sessions/{adw_id}/{agent}/raw_output.jsonl` (full coding-agent stream), `envelope.json` (the parsed final response), `prompts/` (exactly what was sent), and `context_handoff/` (what agents wrote for each other).

## When a run is stuck

A hung coding agent produces no events at all, so the trace goes quiet rather than red. Read it in this order:

```bash
just phases <adw_id>     # which phase is still `running`
just procs <adw_id>      # what that phase is actually running, with pids
just kill <adw_id>       # stop it — children first, then the workflow
```

`processes` rows with `ended_at IS NULL` are the live ones. If `procs` shows a pi child but the phase has produced no `tool_call` events and its `raw_output.jsonl` is empty, the agent never got started properly — check the model resolves and that nothing is blocking the subprocess, rather than waiting it out. `just kill` verifies each pid still matches the command that was recorded before signalling, because pids get recycled.

A killed run marks itself `stopped`, not `fail`, releases its issue's claim with a "was stopped" comment, and closes its process rows, so the trace never claims work is in flight that is already dead. The Console's Stop, on a running Run's card, runs this same `just kill`.

## Report

Tell the engineer, in order: which chain and which roster you launched (name the config whenever it was not the default), which phase is running now (or which failed), phase statuses in sequence, and for a failure the gate violations or the error verbatim. Remember **every phase defaults to `fail`** — a phase showing `fail` may simply never have completed; `queued` means it never started. Don't dress up a partial run as a success.

For a visual live view, the Console in the skill (`just console` → http://localhost:4600 in the background, `just console-stop` / `just console-status`; or for UI development, `bun run server` :4600 + `bun run dev` :4601) polls this same db — sessions as cards, runs as swim lanes, phases and tool calls drill-in. Its Launch pane starts a non-resuming ADW from a typed prompt: it shows the equivalent `uv run … --adw-id <id>` command to confirm, then spawns exactly that, detached, from the repo root, with the ADW's output in `sessions/<adw_id>/console.log`. A Launch is Starting until its session row appears, and Refused, showing that log's tail, if the ADW exits first. A settled Run's card offers Continue with… the Resuming ADWs: the same form and confirm step, launched with that Run's `--adw-id`, Starting until the Resuming ADW joins the Run's trace. Runs keep going when the Console stops, and a restarted Console shows today's Starting and Refused Launches again from each one's `sessions/<adw_id>/launch.json`, asking `procs.py running` whether its process still runs. A Console-launched Run's trace view offers **Show full log**, the whole `console.log`, so what the ADW printed after its trace began is never lost. Below the form, the Ready issues list (from `issues.py --list-ready --json`, for either Tracker) gives each issue its verdict and reason, links a claimed one to its Run and a Spec to its Tickets, and offers Rerun, or Rerun with `--force` beside a failed Run's open PR. It loads on open, on refresh and after each Launch settles, never on a poll. A repo whose ADWs predate `--describe` gets the Console read-only, with a `just sssf-update` banner. The sqlite queries above remain the headless equivalent. Its names from when it was the visualizer, `just obs`, `obs-stop` and `obs-status`, are aliases kept for one release.
