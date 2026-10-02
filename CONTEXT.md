# Super Simple Software Factory

A factory that runs repeatable agents-plus-code workflows in a target repo: code owns the loop, agents work inside bounded phases, and every event is traced.

## Language

### Workflows

**ADW** (AI Developer Workflow):
A deterministic script that owns sequencing, retries and acceptance for one chain of phases, with coding agents as bounded nodes inside it.
_Avoid_: flow, pipeline, job

**Control plane**:
Whatever owns a run's sequencing, retries and acceptance — in this factory, always the ADW, never the agent and never a UI.
_Avoid_: using it for the Console

**Run**:
One execution of an ADW, identified by its `adw_id`, from request to settled outcome.
_Avoid_: session (that is the trace's storage name), job

**Resuming ADW**:
An ADW that continues an earlier Run's work under that Run's `adw_id` rather than starting from a request.
_Avoid_: follow-up flow

### Issues

**Tracker**:
Where a repo's issues live — GitHub Issues, or local markdown files under `.scratch/` — as named by the repo's `docs/agents/issue-tracker.md`.
_Avoid_: backlog, board

**Spec**:
An issue describing a whole feature; it may be split into Tickets, and once it is, the Tickets are what run.
_Avoid_: PRD, epic

**Ticket**:
An issue cut from a Spec as one vertical slice, carrying its own acceptance criteria and the Tickets that block it.
_Avoid_: task, sub-issue, story

**Ready issue**:
An open issue carrying the repo's ready-for-agent label — the triager's proof that its body may become a prompt.
_Avoid_: approved issue, agent issue

**Runnable**:
A Ready issue the factory will actually start a Run on: not blocked by an open issue, not a spec that has tickets, not already claimed.

**Claim**:
A Run marking an issue as `agent-running` so no second Run takes it, and settling it — outcome comment, ready again or resolved — when the Run ends.
_Avoid_: lock, assign

**Resolved**:
A local Ticket whose Run's work has reached the engineer's starting branch; only Resolved Tickets clear the Tickets they block.
_Avoid_: done, closed (that is GitHub's word, and a merged PR does it there)

### Operating

**Console**:
The local web UI where an engineer launches, watches and stops Runs in one target repo. It triggers ADWs; it never owns their loop.
_Avoid_: control plane, dashboard, launcher

**Launch**:
The Console starting an ADW process for a new Run, with an `adw_id` the Console chose. A Launch exists before its Run has any trace. A Resuming ADW's Launch instead continues a settled Run under that Run's own `adw_id`.

**Starting**:
A Launch whose process is alive but whose Run has not yet appeared in the trace — or, continuing a Run, whose process has not yet joined that Run's trace.

**Refused**:
A Launch whose process exited before its Run ever appeared in the trace (or, continuing a Run, before joining it) — the ADW turned the request down (bad config, unrunnable issue, dirty tree).
_Avoid_: failed (a failed Run has a trace; a Refused Launch has none)
