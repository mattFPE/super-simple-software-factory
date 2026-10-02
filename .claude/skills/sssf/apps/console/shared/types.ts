/**
 * Types shared by the read-only server and the Vue client.
 *
 * Every interface mirrors a table in sssf.db one-for-one (see
 * references/observability.md). Nothing here is derived state: phase durations,
 * session progress and lane layout are computed in the UI, never stored.
 */

/** sessions.status — a run is running until it earns success; `stopped` was ended on request. */
export type SessionStatus = "running" | "success" | "fail" | "stopped";

/** phases.status — queued only for manifest-declared phases not yet entered. */
export type PhaseStatus = "queued" | "running" | "success" | "fail";

/** phases.kind — decides which lane a block renders in. */
export type PhaseKind = "engineer" | "code" | "agent";

/** events.type — the ten types tracer.py emits. */
export type EventType =
  | "phase_start"
  | "phase_end"
  | "agent_start"
  | "agent_end"
  | "tool_call"
  | "handoff"
  | "gate_pass"
  | "gate_fail"
  | "log"
  | "error";

export interface Session {
  adw_id: string;
  /** ADW script(s) that ran this session, e.g. "adw_plan + adw_build_test". */
  adw_name: string | null;
  request: string | null;
  status: SessionStatus | null;
  engineer: string | null;
  started_at: string | null;
  ended_at: string | null;
  total_tokens: number | null;
  total_cost: number | null;
  /** 1 once archived out of the review list. Review state, not run state. */
  archived: number | null;
}

/**
 * A session row with its phases embedded, so the L1 table draws the
 * mini-progress dots without a second request per row.
 */
export interface SessionSummary extends Session {
  /** Full phase rows, ordered by seq — one dot each. */
  phases: Phase[];
  phase_count: number;
  /**
   * The session's agents, same shape and merge rules as SessionDetail.agents —
   * so an L1 card can color its per-agent dots without a request per card.
   */
  agents: AgentSession[];
}

export interface Phase {
  phase_id: string;
  adw_id: string;
  seq: number | null;
  name: string | null;
  kind: PhaseKind | null;
  owner: string | null;
  description: string | null;
  status: PhaseStatus | null;
  attempt: number | null;
  retries: number | null;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface Event {
  /** SQLite rowid — the polling cursor. Monotonic, insertion-ordered. */
  rowid: number;
  event_id: string;
  adw_id: string;
  phase_id: string | null;
  /** Span nesting: an agent phase expands into its tool-call children. */
  parent_id: string | null;
  type: EventType | null;
  name: string | null;
  /** Raw JSON string as written by the tracer; parse at the point of display. */
  payload_json: string | null;
  tokens: number | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface Envelope {
  envelope_id: string;
  adw_id: string;
  phase_id: string | null;
  agent: string | null;
  /** Name of the data_types model the response was parsed against. */
  output_type: string | null;
  payload_json: string | null;
  /** SQLite integer boolean. */
  valid: number | null;
  attempt: number | null;
  created_at: string | null;
}

export interface GateResult {
  id: number;
  adw_id: string;
  phase_id: string | null;
  attempt: number | null;
  gate: string | null;
  /** SQLite integer boolean. */
  passed: number | null;
  /** JSON array of violation strings; "[]" on a pass. */
  violations_json: string | null;
  /**
   * JSON array of GateCheck — the per-item evidence behind the verdict, so a
   * green gate can say WHAT it verified rather than only that it passed.
   * Null on rows written before the tracer recorded checks; those are not
   * backfilled, so fall back to the verdict alone.
   */
  checks_json: string | null;
  created_at: string | null;
}

/** One item a gate inspected — the parsed element of `checks_json`. */
export interface GateCheck {
  item: string;
  ok: boolean;
  note: string;
}

/** agent_sessions — the queryable mirror of agent_map.json. Supplies lane labels (`name · model`). */
export interface AgentSession {
  adw_id: string;
  agent: string;
  coding_agent: string | null;
  model: string | null;
  session_id: string | null;
  /**
   * The agent's lane color from sssf.config.yaml, e.g. "#a78bfa". Null on dbs
   * written by a tracer predating the column, and on agents with no configured
   * color — fall back to the UI's own palette.
   */
  color: string | null;
  /**
   * How full the agent's context window was after its last turn, and the
   * model's ceiling. Null on dbs predating the columns and on an agent still
   * running — the lane draws no bar rather than a misleading empty one.
   */
  context_tokens: number | null;
  context_window: number | null;
  created_at: string | null;
  last_used_at: string | null;
}

// ── payload_json shapes ──────────────────────────────────────────────────────
// events.payload_json is stored as a string. These are the parsed shapes for
// the two payloads the UI renders; every field is optional because the tracer
// writes what the coding agent reported, which varies by agent and by version.

/** Parsed `agent_start` payload — the live source of a lane's label and color. */
export interface AgentStartPayload {
  model?: string;
  thinking?: string;
  session_id?: string;
  color?: string;
  coding_agent?: string;
  purpose?: string;
  /** Tool allowlist; null means all tools. Absent on pre-config-payload rows. */
  tools?: string[] | null;
  harness_engineering?: string[];
}

/**
 * Tokens and dollars per component for one agent phase, summed across every
 * send it made (a retried phase paid more than once). Mirrors pi's `usage`:
 * `input_tokens` EXCLUDES cache reads, which bill at their own rate.
 */
export interface UsageBreakdown {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  /**
   * Thinking tokens — the reasoning SHARE of `output_tokens`, not a fifth
   * component. Billed at the output rate; adding it to the others would
   * double-count. Absent (undefined) on runs predating the field.
   */
  reasoning_tokens?: number;
  total_tokens: number;
  input_cost: number;
  output_cost: number;
  cache_read_cost: number;
  cache_write_cost: number;
  total_cost: number;
}

/** Parsed `agent_end` payload — closes out a call with its cost and context use. */
export interface AgentEndPayload {
  cost?: number;
  /** Absent on runs predating the breakdown; `cost` alone survives there. */
  usage?: UsageBreakdown;
  /** Window occupancy after the final turn, and the model's ceiling. */
  context_tokens?: number;
  context_window?: number;
}

/**
 * Parsed `tool_call` payload — one event per real tool call, emitted when the
 * tool returns. `result_snippet` and `duration_ms` are absent when the coding
 * agent never reported a result.
 */
export interface ToolCallPayload {
  tool?: string;
  tool_call_id?: string;
  args?: Record<string, unknown>;
  result_snippet?: string;
  ok?: boolean;
  duration_ms?: number;
  agent?: string;
}

// ── API responses ────────────────────────────────────────────────────────────

/** GET /api/sessions */
export type SessionsResponse = SessionSummary[];

/** GET /api/sessions/:adw_id */
/**
 * What actually moved through a session, summed across every agent.
 *
 * Deliberately NOT the billed total: `sessions.total_tokens` also counts every
 * cached re-read, which is the same context charged again on each turn.
 */
export interface SessionUsage {
  /** Raw prompt tokens read for the first time: new input + cache writes. */
  read: number;
  /** Tokens generated. Each produced exactly once, so this needs no adjusting. */
  written: number;
}

export interface SessionDetail {
  session: Session;
  /** Derived from agent_end payloads, so historical runs have it too. */
  usage: SessionUsage;
  /** Ordered by seq. */
  phases: Phase[];
  /**
   * One entry per agent that has run OR is running under this adw_id — lane
   * labels come from here. Finished agents come from the agent_sessions table;
   * an agent still in flight has no row there yet, so its entry is built from
   * its agent_start event (coding_agent is null until it finishes).
   */
  agents: AgentSession[];
}

/**
 * GET /api/sessions/:adw_id/events?after=<rowid>&limit=500
 *
 * Poll with `after` = the cursor from the previous response. `cursor` is the
 * highest rowid in this page (or the `after` you sent, when the page is empty),
 * so it can be fed straight back in. `has_more` means the page hit the limit.
 */
export interface EventsPage {
  events: Event[];
  cursor: number;
  has_more: boolean;
}

/**
 * GET /api/sessions/:adw_id/agents/:agent/prompts
 *
 * The exact compiled prompts sent to an agent, read from
 * `{data_dir}/sessions/{adw_id}/{agent}/prompts/`. These live only as files —
 * the db has no copy. Either field is null when that file isn't on disk, which
 * is the normal state for an agent that never ran in this session, so a 200
 * with two nulls is a valid answer rather than an error.
 */
export interface AgentPrompts {
  system: string | null;
  user: string | null;
}

/** Alias matching the naming of the other endpoint payloads. */
export type PromptsResponse = AgentPrompts;

/** GET /api/sessions/:adw_id/envelopes */
export type EnvelopesResponse = Envelope[];

/** GET /api/sessions/:adw_id/gates */
export type GatesResponse = GateResult[];

/** GET /api/health */
export interface HealthResponse {
  ok: boolean;
  /**
   * Identifies this server, so `background.ts stop` never kills a stranger on
   * the port. Still the pre-rename name for one release, because tooling from
   * before the Console reads this field alone; it becomes "sssf-console" next.
   */
  service: "sssf-visualizer";
  /** Every name this server answers to, new first: the Console and, for one release, the visualizer. */
  services: ["sssf-console", "sssf-visualizer"];
  pid: number;
  db: string;
  journal_mode: string;
  sessions: number;
}

export interface ApiError {
  error: string;
}

// ── Launching ────────────────────────────────────────────────────────────────

/** One option of an ADW's command line, as its `--describe` prints it. */
export interface AdwOption {
  /** argparse dest, the key a launch request's `values` uses. */
  name: string;
  /** `--merge`; null for a positional such as the prompt. */
  flag: string | null;
  /** "flag" | "value" | "choice"; anything else renders as a text field. */
  kind: string;
  help: string | null;
  default: unknown;
  choices: string[] | null;
  required: boolean;
}

/** `uv run adws/<adw>.py --describe`. */
export interface AdwDescription {
  commits: boolean;
  /** A Resuming ADW continues an earlier Run, so it is never a fresh Launch. */
  resumes: boolean;
  options: AdwOption[];
  /** Flags of which at most one may be set, e.g. [["--branch", "--merge", "--pr"]]. */
  mutually_exclusive: string[][];
}

export interface AdwInfo {
  /** Script stem, e.g. "adw_plan_build". */
  name: string;
  /** Repo-relative, e.g. "adws/adw_plan_build.py". */
  file: string;
  /** First line of the docstring. */
  summary: string;
  /** The docstring's `Phases:` line, continuation lines joined. */
  phases: string | null;
  /** Null when `--describe` failed; `error` says how. */
  description: AdwDescription | null;
  error: string | null;
  /** `--describe` was turned down as an unknown flag: the ADW is from before it existed. */
  predates_describe: boolean;
}

/** GET /api/adws */
export interface AdwCatalog {
  adws: AdwInfo[];
  /** Every ADW here predates `--describe`, so nothing can launch until the repo updates sssf. */
  read_only: boolean;
  /**
   * The ADW picking an issue selects: the committing, non-resuming one with
   * the most phases, so the default Launch claims the issue and lands a PR
   * that closes it. Null when no ADW here commits.
   */
  default_issue_adw: string | null;
}

/** POST /api/launches/preview and POST /api/launches */
export interface LaunchRequest {
  adw: string;
  /** Keyed by AdwOption.name: text for values and choices, booleans for flags. */
  values: Record<string, string | boolean>;
  /** The id a preview minted; omitted, the launch mints one. */
  adw_id?: string;
  /**
   * The settled Run a Resuming ADW continues, under that Run's own adw_id.
   * Such a Launch always needs its preview: there is no minted id to match.
   */
  continues?: string;
  /**
   * The settled Run whose kept worktree an issue's Rerun picks back up: the
   * same ADW under that Run's own adw_id, as its outcome comment says to type.
   * Like continues, it always needs its preview.
   */
  reruns?: string;
}

/** POST /api/launches/preview: what would run, so the engineer can confirm it. */
export interface LaunchPreview {
  adw_id: string;
  /** Exactly what is spawned, program first. No shell ever sees it. */
  argv: string[];
  /** The same argv quoted for a POSIX shell, to read or paste into a terminal. */
  command: string;
}

/**
 * starting: the process is alive and has no session row yet.
 * refused: the process exited without ever writing one.
 * started: its session row exists, so it is an ordinary Run.
 * A continuing Launch's Run already has a row, so for it read "row" as "its
 * own process recorded in that Run's trace".
 */
export type LaunchState = "starting" | "refused" | "started";

/** GET /api/launches: the Launches this server started, newest first. In memory only. */
export interface Launch extends LaunchPreview {
  adw: string;
  /** A Resuming ADW continuing an existing Run: Starting until it joins that Run's trace. */
  continuing: boolean;
  /** An issue's Rerun under its failed Run's adw_id: Starting, too, until it joins that Run's trace. */
  rerun: boolean;
  state: LaunchState;
  started_at: string;
  exit_code: number | null;
  /** The last lines of the launch log: for a refused Launch, the ADW's own message. */
  log_tail: string;
  /**
   * The issue this Launch's Run will Claim, as its reference (`#42`, or a local
   * issue's path): one its prompt names, for an ADW that commits; else null.
   */
  issue: string | null;
  /**
   * It holds its issue: Starting, or started with its Claim not yet landed —
   * while the Tracker would still call the issue Runnable, so no second
   * Launch of it may start.
   */
  holds_issue: boolean;
}

// ── Ready issues ─────────────────────────────────────────────────────────────

/** Another issue, as `--list-ready` names it. */
export interface IssueLink {
  /** A local Spec has none. */
  number: number | null;
  title: string;
  /** A local issue's is its path. */
  url: string;
  /** A local issue's repo-relative path; null on GitHub. */
  path: string | null;
  state: string;
  labels: string[];
}

/** Why a Ready issue isn't Runnable, as a Launch of it would say: the first of its problems. */
export type IssueVerdict = "runnable" | "blocked" | "spec" | "claimed" | "open_pr" | "closed";

/** The most recent Run whose Claim phase logged this issue, read from the trace. */
export interface IssueRun {
  adw_id: string;
  adw_name: string | null;
  status: SessionStatus | null;
  /** The worktree it logged, while that is still on disk: a failed Run keeps it for a Rerun. */
  worktree: string | null;
  /** The PR it landed, if it got that far. */
  pr: string | null;
}

/**
 * What an issue's failed Run says to do next, in its outcome comment: rerun
 * the same ADW under its adw_id to pick its kept worktree back up, or — when
 * it left an open PR — start afresh with `--force`.
 */
export interface IssueRerun {
  /** The ADW the failed Run ran. */
  adw: string;
  /** The failed Run's adw_id to rerun under; null for a fresh `--force` Launch. */
  adw_id: string | null;
  force: boolean;
  /** The open PR the failed Run left; set exactly when force is. */
  pr: string | null;
}

export interface ReadyIssue {
  /** A local Spec has none: a local issue is named by its path. */
  number: number | null;
  title: string;
  /** A local issue's is its path. */
  url: string;
  /** A local issue's repo-relative path; null on GitHub. */
  path: string | null;
  verdict: IssueVerdict;
  /** What a Launch of it would refuse with; null when it is Runnable. */
  why: string | null;
  /** blocked: the open issues it waits on. */
  blocked_by: IssueLink[];
  /** spec: its Tickets, and the Runnable ones to run instead. */
  tickets: IssueLink[];
  run_instead: IssueLink[];
  /** open_pr: the open PRs that already close it. */
  prs: string[];
  /** Added by the Console from the trace, never by GitHub. */
  run: IssueRun | null;
  /** Offered when its latest Run failed and left a kept worktree or an open PR; a fresh Launch stays available. */
  rerun: IssueRerun | null;
  /** The adw_id of a Launch holding this issue until its Claim lands: it can't be launched again yet. */
  held_by: string | null;
}

/**
 * GET /api/issues: `issues.py --list-ready --json`, Runnable issues first,
 * with each issue's Run and Starting Launch added. Read only when asked —
 * the Console never polls GitHub.
 */
export interface IssueListing {
  tracker: string | null;
  repo: string | null;
  ready_label: string | null;
  available: boolean;
  /** Why issues aren't available, in one line, and what to do about it. */
  unavailable: { reason: string; fix: string } | null;
  issues: ReadyIssue[];
  /** There were more Ready issues than the listing shows. */
  truncated: boolean;
}

/**
 * What became of one live process a Stop found recorded for the Run: `killed`,
 * `stopped` (the ADW settled itself and exited), `gone` (already exited), or
 * `mismatch` — the pid now runs something else, so it was never signalled.
 */
export interface StoppedProcess {
  kind: string;
  name: string;
  pid: number;
  command: string;
  outcome: "killed" | "stopped" | "gone" | "mismatch";
  /** What the pid runs now, when it exists. */
  live_command: string | null;
  /** procs.py's own sentence for what happened to it. */
  said: string;
}

/** POST /api/sessions/:adw_id/stop — the report of `procs.py stop`, as it printed it. */
export interface StopReport {
  adw_id: string;
  status: SessionStatus;
  /** `run`: the Run settled itself; `stop`: it couldn't, and the stop closed its trace. */
  settled_by: "run" | "stop";
  processes: StoppedProcess[];
  notes: string[];
}
