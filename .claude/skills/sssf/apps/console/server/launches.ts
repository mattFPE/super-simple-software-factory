/**
 * Launching ADWs: which ones the repo has, what each accepts, and the
 * processes this server has started.
 *
 * The catalog is the repo's `adws/adw_*.py`, each read for its docstring and
 * asked for its own command line with `--describe`, so a form can never offer
 * a flag the ADW doesn't take. Whether it takes the argv a form builds is the
 * ADW's to say too: a preview asks its `--check-args`, so the rules stay in
 * Python with the parser they belong to. A Launch spawns exactly what the
 * confirm step showed: an argv list, never a shell, from the repo root,
 * detached so the Run outlives this server. Its output goes to `console.log`
 * in the Run's session dir, and beside it `launch.json` records the Launch
 * itself; those two files are all the Console writes for a Launch. The trace
 * is the ADW's own tracer's to write (ADR 0001).
 *
 * A Resuming ADW is launched only to continue a settled Run, under that Run's
 * adw_id, and its Launch is Starting until its own process joins that Run's
 * trace. An issue's Rerun joins its failed Run the same way, with the ADW that
 * Run ran, so it picks the kept worktree back up.
 *
 * After a restart a Run is still found through its session row, and a Launch
 * that never became its Run through its record: today's, unless dismissed.
 * This server no longer holds that process, so whether it still runs is asked
 * of procs.py, the check `just kill` makes against a recycled pid; with no
 * exit to see, its exit code is unknown. A check that can't answer leaves the
 * Launch as it was, to be asked again.
 */
import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  appendFileSync, closeSync, existsSync, fstatSync, mkdirSync, openSync, readdirSync, readFileSync, readSync, statSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import type {
  AdwCatalog, AdwDescription, AdwInfo, AdwOption, Launch, LaunchPreview, LaunchRequest, Session, Shell,
} from "../shared/types.ts";
import { LOCAL_TRACKER, claimingAdw, issueNamed } from "../shared/issues.ts";

/** What Launches read back from the trace, which only the ADWs write (ADR 0001). */
export interface Trace {
  session(adwId: string): Pick<Session, "status" | "adw_name"> | null;
  /** ADW processes recorded under a Run: one more means a Resuming ADW has joined it. */
  adwProcessCount(adwId: string): number;
  /** How often a Run's Claim phase has logged its issue: until it logs again, nothing on the Tracker says it's taken. */
  claimCount(adwId: string): number;
}

/** An error the route turns into this HTTP status rather than a 500. */
export class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

/**
 * The target repo: --repo, then SSSF_REPO, then the git checkout holding the
 * db. ADWs run from here, because their default config path is relative to it.
 * Before the first Run the db's own dir may not exist yet, so git is asked from
 * the nearest dir above it that does.
 */
export function resolveRepoRoot(dbPath: string, argv: string[] = Bun.argv): string {
  const flagIndex = argv.indexOf("--repo");
  const raw = (flagIndex !== -1 ? argv[flagIndex + 1] : undefined) ?? process.env.SSSF_REPO;
  if (raw) return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
  let dir = dirname(dbPath);
  while (!existsSync(dir) && dirname(dir) !== dir) dir = dirname(dir);
  const git = spawnSync("git", ["rev-parse", "--show-toplevel"],
    { cwd: dir, encoding: "utf8", windowsHide: true });
  return git.status === 0 && git.stdout.trim() ? resolve(git.stdout.trim()) : process.cwd();
}

const ADW_FILE = /^adw_[A-Za-z0-9_]+\.py$/;
const ADW_ID = /^[A-Za-z0-9_-]{1,64}$/;
const ASK_ADW_TIMEOUT_MS = 120_000;   // a first `uv run` may resolve the script's deps
const TAIL_LINES = 20;
const TAIL_BYTES = 16 * 1024;
const RECORD = "launch.json";
/** How often a recovered Launch's process is asked after: each ask is a `uv run`. */
const RECHECK_MS = 3_000;

const indentOf = (line: string): number => line.length - line.trimStart().length;

/** The module docstring's first line and its `Phases:` line, continuation lines joined. */
function readDocstring(source: string): { summary: string; phases: string | null } {
  // Past the shebang and the script's dependency block. `[^\n]`, not `.`, which stops at a CRLF's \r.
  const doc = /^(?:\s*#[^\n]*\n|\s*\n)*\s*(?:"""|''')([\s\S]*?)(?:"""|''')/.exec(source)?.[1] ?? "";
  const lines = doc.split(/\r?\n/);
  const summary = lines.find((line) => line.trim())?.trim() ?? "";
  const start = lines.findIndex((line) => /^\s*Phases:/.test(line));
  if (start === -1) return { summary, phases: null };
  const parts = [lines[start]!.replace(/^\s*Phases:/, "").trim()];
  for (const line of lines.slice(start + 1)) {
    if (!line.trim() || indentOf(line) <= indentOf(lines[start]!)) break;
    parts.push(line.trim());
  }
  return { summary, phases: parts.join(" ").replace(/\s+/g, " ") };
}

/**
 * How many phases a `Phases:` line names, for the default issue ADW: the
 * longest committing chain. A retry loop, `[-> builder(fix) -> code(test) ...
 * bounded]`, repeats phases already counted, so it adds none.
 */
const phaseCount = (phases: string | null): number =>
  (phases ?? "").replace(/\[\s*->[^\]]*\]/g, "").split("->").filter((p) => p.replace(/[[\]]/g, "").trim()).length;

function isDescription(value: unknown): value is AdwDescription {
  const d = value as AdwDescription;
  return typeof d === "object" && d !== null && Array.isArray(d.options) && Array.isArray(d.mutually_exclusive);
}

/** `--describe` failed. Exit 2 is argparse refusing the flag: an ADW from before it existed. */
class DescribeFailed extends Error {
  constructor(readonly exitCode: number | null, message: string) {
    super(message);
  }
}

/** Ask an ADW something that starts nothing (`--describe`, `--check-args`): its output, its last stderr line and its exit. */
async function askAdw(
  repoRoot: string, file: string, args: string[],
): Promise<{ out: string; said: string; code: number | null }> {
  const proc = Bun.spawn(["uv", "run", file, ...args], {
    cwd: repoRoot, stdout: "pipe", stderr: "pipe", windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  const timer = setTimeout(() => proc.kill(), ASK_ADW_TIMEOUT_MS);
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited,
  ]).finally(() => clearTimeout(timer));
  return { out, said: err.trim().split(/\r?\n/).at(-1)?.trim() ?? "", code };
}

async function runDescribe(repoRoot: string, file: string): Promise<AdwDescription> {
  const { out, said, code } = await askAdw(repoRoot, file, ["--describe"]);
  if (code !== 0) throw new DescribeFailed(code, `--describe exited ${code}${said ? `: ${said}` : ""}`);
  const parsed: unknown = JSON.parse(out);
  if (!isDescription(parsed)) throw new DescribeFailed(0, "--describe printed something other than an ADW description");
  return parsed;
}

/**
 * Ask the ADW whether it takes the arguments of `argv`, as a Launch of them
 * would: its own parser, and the checks it makes before starting. Nothing
 * starts. One it turns down is a 400 with the ADW's own message.
 */
async function checkArgs(repoRoot: string, argv: string[]): Promise<void> {
  const [file, ...args] = adwCommand(argv);
  const { said, code } = await askAdw(repoRoot, file!, ["--check-args", ...args]);
  if (code !== 0) throw new HttpError(400, said || `${file} --check-args exited ${code}`);
}

/** A Launch's argv past `uv run`: the ADW's file and its arguments. */
const adwCommand = (argv: string[]): string[] => argv.slice(2);

/**
 * POSIX quoting, so the shown command pastes into a terminal and means the
 * same argv. An issue reference is double-quoted, `"#42"`, as the ADWs' own
 * outcome comments write it; anything else that needs quoting is single-quoted.
 */
export function shellQuote(arg: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(arg)) return arg;
  return /^[A-Za-z0-9_@%+=:,./#-]+$/.test(arg) ? `"${arg}"` : `'${arg.replace(/'/g, `'\\''`)}'`;
}

/**
 * PowerShell quoting, for PowerShell 7.3+, which hands a native program each
 * argument as written, embedded `"` included. Anything but a plain word or
 * flag name is single-quoted: PowerShell reads `~`, `@`, `$` and `,` as syntax
 * and splits `-a.b` in two. Inside single quotes only a quote is special, and
 * PowerShell takes the typographic ones for `'` too, so each is doubled.
 */
export function powershellQuote(arg: string): string {
  if (/^[A-Za-z0-9_./=:+][A-Za-z0-9_./=:+-]*$|^-+([A-Za-z0-9][A-Za-z0-9-]*)?$/.test(arg)) return arg;
  return `'${arg.replace(/['‘’‚‛]/g, "$&$&")}'`;
}

/** The shell whose command the confirm step selects first: the one the server's platform runs. */
const PLATFORM_SHELL: Shell = process.platform === "win32" ? "powershell" : "posix";

/** PowerShell drops an argument that is exactly `--%`, however it is quoted: no command of its passes one. */
function commandsFor(argv: string[]): Pick<LaunchPreview, "commands" | "shell"> {
  const powershell = argv.includes("--%") ? null : argv.map(powershellQuote).join(" ");
  return { commands: { posix: argv.map(shellQuote).join(" "), powershell }, shell: PLATFORM_SHELL };
}

/**
 * The ADW's arguments for these form values: its options in the order `--describe`
 * listed them, the minted id, then `--` and the positionals. After `--` argparse
 * reads a prompt such as "--help" as text, and a value that starts with "-" goes
 * as `--flag=value` for the same reason. An issue reference, which can't be read
 * as an option, goes first instead: `"#42" --adw-id …` or `.scratch/…/03-x.md
 * --adw-id …`, the shape the ADWs' outcome comments give for a rerun.
 *
 * Only the argv is built here. Whether the ADW takes it — a choice, flags that
 * exclude each other, a missing prompt — is its own parser's to say (`checkArgs`).
 */
function argsFor(
  info: AdwInfo, description: AdwDescription, values: LaunchRequest["values"], adwId: string, tracker: string | null,
): string[] {
  const known = new Map(description.options.map((o) => [o.name, o]));
  for (const name of Object.keys(values)) {
    if (name === "adw_id") throw new HttpError(400, "adw_id is minted by the Console, not set in the form");
    if (!known.has(name)) throw new HttpError(400, `${info.name} has no option ${name}`);
  }

  const value = (o: AdwOption): string | null => {
    const raw = values[o.name];
    if (raw === undefined || raw === "" || raw === false) return null;
    if (typeof raw !== "string") throw new HttpError(400, `${o.name} takes text, not ${JSON.stringify(raw)}`);
    return raw;
  };

  const positionals: string[] = [];
  for (const o of description.options.filter((option) => option.flag === null)) {
    const v = value(o);
    if (v !== null) positionals.push(v);
  }
  const args: string[] = [];
  for (const o of description.options.filter((option) => option.flag !== null && option.name !== "adw_id")) {
    if (o.kind === "flag") {
      if (values[o.name] !== undefined && typeof values[o.name] !== "boolean") {
        throw new HttpError(400, `${o.flag} is a flag: true or false`);
      }
      if (values[o.name] === true) args.push(o.flag!);
      continue;
    }
    const v = value(o);
    if (v !== null) args.push(...(v.startsWith("-") ? [`${o.flag}=${v}`] : [o.flag!, v]));
  }
  args.push("--adw-id", adwId);
  if (positionals.length === 1 && issueNamed(positionals[0], tracker) !== null) return [positionals[0]!, ...args];
  return positionals.length ? [...args, "--", ...positionals] : args;
}

/**
 * The last lines of a log from byte `from` on, read from its end so a long
 * Run's output costs nothing. A continuing Launch appends to its Run's log, so
 * its tail starts where it began.
 */
function tailOf(path: string, from: number): string {
  if (!existsSync(path)) return "";
  const fd = openSync(path, "r");
  try {
    const size = fstatSync(fd).size;
    const length = Math.max(0, Math.min(size - from, TAIL_BYTES));
    const buffer = Buffer.alloc(length);
    readSync(fd, buffer, 0, length, size - length);
    return buffer.toString("utf8").replace(/\s+$/, "").split(/\r?\n/).slice(-TAIL_LINES).join("\n");
  } finally {
    closeSync(fd);
  }
}

/** A Launch this server spawned, or recovered from its record, and whether its process has exited. */
interface Tracked {
  launch: Omit<Launch, "state" | "log_tail" | "holds_issue">;
  exited: boolean;
  /** Where this Launch's output begins in the log. */
  logFrom: number;
  /** For a Launch joining a Run (continuing, or a Rerun), the Run's ADW process count when it began; null for a fresh one. */
  processesBefore: number | null;
  /** The Run's Claims logged when it began: its own Claim is the next one. */
  claimsBefore: number;
  /** The process spawned; null when it never started. */
  pid: number | null;
  /** Read back from its record: no exit event will come, so procs.py is asked instead. */
  recovered: boolean;
  checkedAt: number;
  checking: boolean;
}

/** `launch.json`: what a restarted server needs to show a Launch again. */
interface LaunchRecord {
  adw_id: string;
  adw: string;
  argv: string[];
  /** The command the confirm step showed, for whoever reads the file: argv is what runs and is checked. */
  command: string;
  started_at: string;
  pid: number | null;
  continuing: boolean;
  rerun: boolean;
  issue: string | null;
  exited: boolean;
  exit_code: number | null;
  log_from: number;
  processes_before: number | null;
  claims_before: number;
  dismissed: boolean;
}

const int = <T>(value: unknown, fallback: T): number | T => (Number.isInteger(value) ? value as number : fallback);

/** A record as written, or null when it is missing or isn't one; optional fields fall back. */
function readRecord(path: string, adwId: string): LaunchRecord | null {
  let raw: Partial<LaunchRecord>;
  try {
    raw = JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
  const { argv, pid } = raw;
  if (raw.adw_id !== adwId || typeof raw.adw !== "string" || typeof raw.started_at !== "string"
    || !Array.isArray(argv) || !argv.every((a) => typeof a === "string")
    || !(pid === null || Number.isInteger(pid))) return null;
  return {
    adw_id: adwId, adw: raw.adw, argv, started_at: raw.started_at, pid: pid ?? null,
    command: typeof raw.command === "string" ? raw.command : "",
    continuing: raw.continuing === true,
    rerun: raw.rerun === true,
    issue: typeof raw.issue === "string" ? raw.issue : null,
    exited: raw.exited === true,
    exit_code: int(raw.exit_code, null),
    log_from: int(raw.log_from, 0),
    processes_before: int(raw.processes_before, null),
    claims_before: int(raw.claims_before, 0),
    dismissed: raw.dismissed === true,
  };
}

/** Today, in the server's time zone: the Runs pane's window. */
function startOfToday(): number {
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  return midnight.getTime();
}

export class Launches {
  private readonly entries = new Map<string, Tracked>();
  /**
   * The argv each preview showed, by its minted id: a Launch runs only what was
   * confirmed. `checked` when the ADW's `--check-args` took it then, so its Launch isn't asked again.
   */
  private readonly previewed = new Map<string, { argv: string[]; checked: boolean }>();
  /**
   * The repo's Tracker, as its last `--list-ready` named it: in a Local Markdown
   * repo a prompt that is a local issue's path names that issue. Null until a
   * listing names one; a path prompt before then asks for a listing first.
   */
  private tracker: string | null = null;
  /** --describe output per ADW, kept until the file or the shared CLI module changes. */
  private readonly described = new Map<string, { stamp: string; info: AdwInfo }>();

  constructor(
    readonly repoRoot: string,
    private readonly sessionsDir: string,
    private readonly trace: Trace,
    /** The Tracker issues.py names, when no listing has named it yet. */
    private readonly readTracker: () => Promise<string | null>,
    /** Whether a pid still runs the argv it was spawned with, as procs.py decides it. */
    private readonly stillRunning: (pid: number, argv: string[], adwId: string) => Promise<boolean>,
  ) {}

  /**
   * Show again today's Launches that never became their Run and weren't
   * dismissed, oldest first, each one's process asked after once before the
   * first request is served. Nothing but their records is read.
   */
  async recover(): Promise<void> {
    if (!existsSync(this.sessionsDir)) return;
    const since = startOfToday();
    const records = readdirSync(this.sessionsDir)
      .filter((id) => ADW_ID.test(id))
      .map((id) => readRecord(join(this.sessionsDir, id, RECORD), id))
      .filter((r): r is LaunchRecord => r !== null && !r.dismissed && Date.parse(r.started_at) >= since)
      .toSorted((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at));
    const entries = records.map((r): Tracked => ({
      launch: {
        adw_id: r.adw_id, argv: r.argv, ...commandsFor(r.argv), adw: r.adw, continuing: r.continuing,
        rerun: r.rerun, started_at: r.started_at, exit_code: r.exit_code, issue: r.issue,
      },
      exited: r.exited, logFrom: r.log_from, processesBefore: r.processes_before, claimsBefore: r.claims_before,
      pid: r.pid, recovered: true, checkedAt: 0, checking: false,
    })).filter((entry) => this.view(entry, false).state !== "started");
    await Promise.all(entries.filter((entry) => !entry.exited).map((entry) => this.check(entry)));
    for (const entry of entries) this.entries.set(entry.launch.adw_id, entry);
  }

  /** Ask procs.py whether a recovered Launch's process still runs; once it doesn't, its record says so. */
  private async check(entry: Tracked): Promise<void> {
    entry.checking = true;
    try {
      const running = entry.pid !== null
        && await this.stillRunning(entry.pid, entry.launch.argv, entry.launch.adw_id);
      if (!running) {
        entry.exited = true;
        this.save(entry);
      }
    } catch (error) {
      // Unknown isn't exited: calling it Refused would free its issue for a second Launch. Asked again later.
      console.error(`[sssf] could not tell whether Launch ${entry.launch.adw_id} still runs:`, (error as Error).message);
    } finally {
      entry.checking = false;
      entry.checkedAt = Date.now();
    }
  }

  /** Write a Launch's record beside its log. A failed write costs only its recovery, never the Launch. */
  private save(entry: Tracked, dismissed = false): void {
    const { launch } = entry;
    const record: LaunchRecord = {
      adw_id: launch.adw_id, adw: launch.adw, argv: launch.argv, command: launch.commands.posix,
      started_at: launch.started_at, pid: entry.pid, continuing: launch.continuing, rerun: launch.rerun,
      issue: launch.issue, exited: entry.exited, exit_code: launch.exit_code, log_from: entry.logFrom,
      processes_before: entry.processesBefore, claims_before: entry.claimsBefore, dismissed,
    };
    try {
      writeFileSync(join(this.sessionsDir, launch.adw_id, RECORD), `${JSON.stringify(record, null, 2)}\n`, "utf8");
    } catch (error) {
      console.error(`[sssf] could not record Launch ${launch.adw_id}:`, (error as Error).message);
    }
  }

  /** What a listing said the Tracker is; one that couldn't say leaves the last answer. */
  learnTracker(tracker: string | null): void {
    if (tracker) this.tracker = tracker;
  }

  async catalog(): Promise<AdwCatalog> {
    const dir = join(this.repoRoot, "adws");
    const files = existsSync(dir) ? readdirSync(dir).filter((f) => ADW_FILE.test(f)).toSorted() : [];
    const shared = join(dir, "adw_modules", "session.py");
    const sharedStamp = existsSync(shared) ? String(statSync(shared).mtimeMs) : "";
    const adws = await Promise.all(files.map(async (f) => {
      const path = join(dir, f);
      const stat = statSync(path);
      const stamp = `${stat.mtimeMs}:${stat.size}:${sharedStamp}`;
      const cached = this.described.get(f);
      if (cached?.stamp === stamp) return cached.info;
      const info = await this.describe(f, readFileSync(path, "utf8"));
      this.described.set(f, { stamp, info });
      return info;
    }));
    const issueAdw = adws
      .filter((a) => a.description?.commits && !a.description.resumes)
      .reduce<AdwInfo | null>((best, a) => (!best || phaseCount(a.phases) > phaseCount(best.phases) ? a : best), null);
    return {
      adws,
      read_only: adws.length > 0 && adws.every((a) => a.predates_describe),
      default_issue_adw: issueAdw?.name ?? null,
    };
  }

  private async describe(f: string, source: string): Promise<AdwInfo> {
    const file = `adws/${f}`;
    const base = { name: f.replace(/\.py$/, ""), file, ...readDocstring(source) };
    try {
      return { ...base, description: await runDescribe(this.repoRoot, file), error: null, predates_describe: false };
    } catch (error) {
      const predates = error instanceof DescribeFailed && error.exitCode === 2;
      return { ...base, description: null, error: (error as Error).message, predates_describe: predates };
    }
  }

  /** What a Launch would spawn. Nothing starts; the argv is kept for the Launch to match. */
  async preview(req: LaunchRequest): Promise<LaunchPreview> {
    const { issue: _issue, checksArgs, ...shown } = await this.plan(req);
    if (checksArgs) await checkArgs(this.repoRoot, shown.argv);
    this.previewed.set(shown.adw_id, { argv: shown.argv, checked: checksArgs });
    return shown;
  }

  /**
   * What these values would spawn, and whether the ADW can be asked if it takes
   * them: one installed before `--check-args` is left to refuse them at Launch.
   */
  private async plan(
    req: LaunchRequest,
  ): Promise<LaunchPreview & { issue: string | null; checksArgs: boolean }> {
    if (typeof req?.adw !== "string" || typeof req.values !== "object" || req.values === null) {
      throw new HttpError(400, "a launch needs an adw and its values");
    }
    const { continues, reruns } = req;
    for (const [field, runId] of [["continues", continues], ["reruns", reruns]] as const) {
      if (runId === undefined) continue;
      if (typeof runId !== "string" || !ADW_ID.test(runId)) throw new HttpError(400, `invalid ${field}`);
      if (req.adw_id !== undefined || (continues !== undefined && reruns !== undefined)) {
        throw new HttpError(400, `a Launch that ${field} a Run runs under the Run's own adw_id: send ${field} alone`);
      }
    }
    const catalog = await this.catalog();
    if (catalog.read_only) {
      throw new HttpError(409, "this repo's ADWs predate --describe: run `just sssf-update` to launch from the Console");
    }
    const info = catalog.adws.find((a) => a.name === req.adw);
    if (!info) throw new HttpError(404, `no ADW ${req.adw} in adws/`);
    if (!info.description) throw new HttpError(409, `${info.name} can't describe itself: ${info.error}`);
    if (info.description.resumes && continues === undefined) {
      throw new HttpError(400, `${info.name} is a Resuming ADW: it continues an earlier Run, so it can't start a fresh one`);
    }
    if (!info.description.resumes && continues !== undefined) {
      throw new HttpError(400, `${info.name} isn't a Resuming ADW: it starts a fresh Run, so it can't continue ${continues}`);
    }

    const joins = continues ?? reruns;
    const adwId = joins ?? req.adw_id ?? randomBytes(4).toString("hex");
    if (!ADW_ID.test(adwId)) throw new HttpError(400, "invalid adw_id");
    if (joins !== undefined) {
      const verb = continues !== undefined ? "continue" : "rerun";
      const run = this.trace.session(joins);
      if (!run) throw new HttpError(404, `no Run ${joins} to ${verb}`);
      const entry = this.entries.get(joins);
      if (run.status === "running" || (entry && !entry.exited)) {
        throw new HttpError(409, `${joins} is still running: ${verb} it once it has settled`);
      }
      // A Rerun picks up the failed Run's worktree, so it runs the ADW that claimed the issue.
      const ran = claimingAdw(run.adw_name);
      if (reruns !== undefined && ran !== info.name) {
        throw new HttpError(400,
          `${reruns} was a Run of ${ran ?? "an unrecorded ADW"}: rerun it with that ADW, or launch ${info.name} afresh`);
      }
    } else if (this.entries.has(adwId) || this.trace.session(adwId)) {
      throw new HttpError(409, `${adwId} is already a Run`);
    }
    // Only a prompt that would be a local issue needs the Tracker: in a GitHub repo it is a request file.
    const text = req.values[info.description.options.find((o) => o.flag === null)?.name ?? ""];
    if (this.tracker === null && issueNamed(text, LOCAL_TRACKER)?.startsWith(".scratch/")) {
      this.learnTracker(await this.readTracker());
    }
    const argv = ["uv", "run", info.file, ...argsFor(info, info.description, req.values, adwId, this.tracker)];
    const prompt = info.description.options.find((o) => o.flag === null);
    // Only an ADW that commits claims its issue; one that commits nothing leaves it as it was.
    const claims = continues === undefined && info.description.commits && prompt;
    const issue = claims ? issueNamed(req.values[prompt.name], this.tracker) : null;
    // Two Launches of one issue would race before the first one's Claim lands.
    const holder = issue === null ? undefined : this.heldIssues().get(issue);
    if (holder !== undefined) {
      throw new HttpError(409, `${issue} is already being launched as ${holder}: wait until its Run has claimed it`);
    }
    return {
      adw_id: adwId, argv, ...commandsFor(argv), issue,
      checksArgs: info.description.checks_args === true,
    };
  }

  /**
   * Spawn the ADW. With the id a preview minted, or the Run it continues, only
   * that preview's exact argv runs: a form edited after its review is turned
   * down, not launched unseen.
   */
  async start(req: LaunchRequest): Promise<Launch> {
    const { checksArgs, ...preview } = await this.plan(req);
    const continuing = req.continues !== undefined;
    const rerun = req.reruns !== undefined;
    let checked = false;
    if (req.adw_id !== undefined || continuing || rerun) {
      const shown = this.previewed.get(preview.adw_id);
      if (!shown || shown.argv.join("\0") !== preview.argv.join("\0")) {
        throw new HttpError(409, "this isn't the command you reviewed: review the new one before launching it");
      }
      checked = shown.checked;
      this.previewed.delete(preview.adw_id);
    }
    // A Launch no checked preview showed is asked now, before anything spawns.
    if (checksArgs && !checked) await checkArgs(this.repoRoot, preview.argv);
    mkdirSync(join(this.sessionsDir, preview.adw_id), { recursive: true });
    const logPath = this.logFile(preview.adw_id);
    // The Run's earlier output stays above; the full log says where this Launch begins.
    if (continuing || rerun) {
      appendFileSync(logPath, `\n[console] ${continuing ? "continuing" : "rerunning"} with ${preview.commands.posix}\n`, "utf8");
    }
    const fd = openSync(logPath, "a");
    const entry: Tracked = {
      launch: { ...preview, adw: req.adw, continuing, rerun, started_at: new Date().toISOString(), exit_code: null },
      exited: false,
      logFrom: fstatSync(fd).size,
      processesBefore: continuing || rerun ? this.trace.adwProcessCount(preview.adw_id) : null,
      claimsBefore: this.trace.claimCount(preview.adw_id),
      pid: null, recovered: false, checkedAt: 0, checking: false,
    };
    // Re-inserted, so a continued Run's Launch lists as the newest.
    this.entries.delete(preview.adw_id);
    this.entries.set(preview.adw_id, entry);
    try {
      // Detached, output to the log, handle released: the Run outlives this server.
      const child = spawn(preview.argv[0]!, preview.argv.slice(1), {
        cwd: this.repoRoot, detached: true, windowsHide: true, stdio: ["ignore", fd, fd],
        // Unbuffered, or stdout to a file lags stderr and the log reads out of order:
        // a refusal's message would land above the output that led to it.
        env: { ...process.env, PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" },
      });
      child.on("exit", (code) => {
        entry.launch.exit_code = code;
        entry.exited = true;
        this.save(entry);
      });
      child.on("error", (error) => {   // e.g. uv not on PATH: the Launch is Refused, and says why
        appendFileSync(logPath, `[console] could not start: ${error.message}\n`, "utf8");
        entry.exited = true;
        this.save(entry);
      });
      child.unref();
      entry.pid = child.pid ?? null;
    } finally {
      closeSync(fd);
    }
    this.save(entry);
    return this.view(entry);
  }

  /** Each issue a Launch holds until its Run's Claim lands, with that Launch's adw_id. */
  heldIssues(): Map<string, string> {
    const held = new Map<string, string>();
    for (const entry of this.entries.values()) {
      if (entry.launch.issue !== null && this.view(entry).holds_issue) held.set(entry.launch.issue, entry.launch.adw_id);
    }
    return held;
  }

  /**
   * Forget a Refused Launch, so a run of bad ones doesn't bury the Runs. Its
   * log stays in its session dir and the trace is untouched (ADR 0001); its
   * record says it was dismissed, so a restart forgets it too. A Starting one
   * is still running: stopping it isn't dismissing it.
   */
  dismiss(adwId: string): void {
    const entry = this.entries.get(adwId);
    if (!entry) throw new HttpError(404, `no Launch ${adwId}`);
    const { state } = this.view(entry);
    if (state === "starting") throw new HttpError(409, `${adwId} is still starting: only a Refused Launch can be dismissed`);
    if (state !== "refused") throw new HttpError(409, `${adwId} became its Run: only a Refused Launch can be dismissed`);
    this.save(entry, true);
    this.entries.delete(adwId);
  }

  list(): Launch[] {
    return [...this.entries.values()].toReversed().map((entry) => this.view(entry));
  }

  /** The full launch log, or null when this id has none. */
  logPath(adwId: string): string | null {
    if (!ADW_ID.test(adwId)) return null;
    const path = this.logFile(adwId);
    return existsSync(path) ? path : null;
  }

  private logFile(adwId: string): string {
    return join(this.sessionsDir, adwId, "console.log");
  }

  /** With `recheck`, a recovered Launch still thought alive is asked after again, at most every RECHECK_MS. */
  private view(entry: Tracked, recheck = true): Launch {
    const { adw_id } = entry.launch;
    if (recheck && entry.recovered && !entry.exited && !entry.checking && Date.now() - entry.checkedAt > RECHECK_MS) {
      void this.check(entry);
    }
    // The trace is checked first: an ADW that joined it and then exited is a Run, not a refusal.
    const joined = entry.processesBefore === null
      ? this.trace.session(adw_id) !== null
      : this.trace.adwProcessCount(adw_id) > entry.processesBefore;
    const state = joined ? "started" : entry.exited ? "refused" : "starting";
    // Between its row appearing and its Claim landing, the Tracker still calls the issue Runnable.
    const unclaimed = state === "started" && !entry.exited
      && this.trace.session(adw_id)?.status === "running" && this.trace.claimCount(adw_id) <= entry.claimsBefore;
    const holds_issue = entry.launch.issue !== null && (state === "starting" || unclaimed);
    const path = this.logPath(adw_id);
    return {
      ...entry.launch, state, holds_issue,
      log_tail: state === "started" || !path ? "" : tailOf(path, entry.logFrom),
    };
  }
}
