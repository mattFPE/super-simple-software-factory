/**
 * Launching ADWs: which ones the repo has, what each accepts, and the
 * processes this server has started.
 *
 * The catalog is the repo's `adws/adw_*.py`, each read for its docstring and
 * asked for its own command line with `--describe`, so a form can never offer
 * a flag the ADW doesn't take. A Launch spawns exactly what the confirm step
 * showed: an argv list, never a shell, from the repo root, detached so the Run
 * outlives this server. Its output goes to `console.log` in the Run's session
 * dir, the one thing the Console writes for a Launch; the trace is the ADW's
 * own tracer's to write (ADR 0001).
 *
 * Launches live in memory only. After a restart a Run is still found through
 * its session row; one that never wrote a row is simply forgotten.
 */
import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  appendFileSync, closeSync, existsSync, fstatSync, mkdirSync, openSync, readdirSync, readFileSync, readSync, statSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import type {
  AdwCatalog, AdwDescription, AdwInfo, AdwOption, Launch, LaunchPreview, LaunchRequest,
} from "../shared/types.ts";

/** An error the route turns into this HTTP status rather than a 500. */
export class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

/**
 * The target repo: --repo, then SSSF_REPO, then the git checkout holding the
 * db. ADWs run from here, because their default config path is relative to it.
 */
export function resolveRepoRoot(dbPath: string, argv: string[] = Bun.argv): string {
  const flagIndex = argv.indexOf("--repo");
  const raw = (flagIndex !== -1 ? argv[flagIndex + 1] : undefined) ?? process.env.SSSF_REPO;
  if (raw) return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
  const git = spawnSync("git", ["rev-parse", "--show-toplevel"],
    { cwd: dirname(dbPath), encoding: "utf8", windowsHide: true });
  return git.status === 0 && git.stdout.trim() ? resolve(git.stdout.trim()) : process.cwd();
}

const ADW_FILE = /^adw_[A-Za-z0-9_]+\.py$/;
const ADW_ID = /^[A-Za-z0-9_-]{1,64}$/;
const DESCRIBE_TIMEOUT_MS = 120_000;   // a first `uv run` may resolve the script's deps
const TAIL_LINES = 20;
const TAIL_BYTES = 16 * 1024;

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

async function runDescribe(repoRoot: string, file: string): Promise<AdwDescription> {
  const proc = Bun.spawn(["uv", "run", file, "--describe"], {
    cwd: repoRoot, stdout: "pipe", stderr: "pipe", windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  const timer = setTimeout(() => proc.kill(), DESCRIBE_TIMEOUT_MS);
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited,
  ]).finally(() => clearTimeout(timer));
  if (code !== 0) {
    const said = err.trim().split(/\r?\n/).at(-1) ?? "";
    throw new DescribeFailed(code, `--describe exited ${code}${said ? `: ${said}` : ""}`);
  }
  const parsed: unknown = JSON.parse(out);
  if (!isDescription(parsed)) throw new DescribeFailed(0, "--describe printed something other than an ADW description");
  return parsed;
}

/** POSIX single-quoting, so the shown command pastes into a terminal and means the same argv. */
export function shellQuote(arg: string): string {
  return /^[A-Za-z0-9_@%+=:,./-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, `'\\''`)}'`;
}

/**
 * The ADW's arguments for these form values: its options in the order `--describe`
 * listed them, the minted id, then `--` and the positionals. After `--` argparse
 * reads a prompt such as "--help" as text, and a value that starts with "-" goes
 * as `--flag=value` for the same reason.
 */
function argsFor(info: AdwInfo, description: AdwDescription, values: LaunchRequest["values"], adwId: string): string[] {
  const known = new Map(description.options.map((o) => [o.name, o]));
  for (const name of Object.keys(values)) {
    if (name === "adw_id") throw new HttpError(400, "adw_id is minted by the Console, not set in the form");
    if (!known.has(name)) throw new HttpError(400, `${info.name} has no option ${name}`);
  }
  for (const group of description.mutually_exclusive) {
    const set = description.options.filter((o) => o.flag && group.includes(o.flag) && values[o.name] === true);
    if (set.length > 1) throw new HttpError(400, `${set.map((o) => o.flag).join(" and ")} can't be used together`);
  }

  const value = (o: AdwOption): string | null => {
    const raw = values[o.name];
    if (raw === undefined || raw === "" || raw === false) return null;
    if (typeof raw !== "string") throw new HttpError(400, `${o.name} takes text, not ${JSON.stringify(raw)}`);
    if (o.kind === "choice" && o.choices && !o.choices.includes(raw)) {
      throw new HttpError(400, `${o.flag ?? o.name} is one of ${o.choices.join(", ")}, not ${raw}`);
    }
    return raw;
  };

  const positionals: string[] = [];
  for (const o of description.options.filter((option) => option.flag === null)) {
    const v = value(o);
    if (v === null && o.required) throw new HttpError(400, `${o.name} is required`);
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
  return positionals.length ? [...args, "--", ...positionals] : args;
}

/** The last lines of a log, read from its end so a long Run's output costs nothing. */
function tailOf(path: string): string {
  if (!existsSync(path)) return "";
  const fd = openSync(path, "r");
  try {
    const size = fstatSync(fd).size;
    const length = Math.min(size, TAIL_BYTES);
    const buffer = Buffer.alloc(length);
    readSync(fd, buffer, 0, length, size - length);
    return buffer.toString("utf8").replace(/\s+$/, "").split(/\r?\n/).slice(-TAIL_LINES).join("\n");
  } finally {
    closeSync(fd);
  }
}

/** A Launch this server spawned, and whether its process has exited. */
interface Tracked {
  launch: Omit<Launch, "state" | "log_tail">;
  exited: boolean;
}

export class Launches {
  private readonly entries = new Map<string, Tracked>();
  /** The argv each preview showed, by its minted id: a Launch runs only what was confirmed. */
  private readonly previewed = new Map<string, string[]>();
  /** --describe output per ADW, kept until the file or the shared CLI module changes. */
  private readonly described = new Map<string, { stamp: string; info: AdwInfo }>();

  constructor(
    readonly repoRoot: string,
    private readonly sessionsDir: string,
    /** Whether the trace has a session row for this id: the moment a Launch becomes a Run. */
    private readonly hasRun: (adwId: string) => boolean,
  ) {}

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
    return { adws, read_only: adws.length > 0 && adws.every((a) => a.predates_describe) };
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
    const preview = await this.plan(req);
    this.previewed.set(preview.adw_id, preview.argv);
    return preview;
  }

  private async plan(req: LaunchRequest): Promise<LaunchPreview> {
    if (typeof req?.adw !== "string" || typeof req.values !== "object" || req.values === null) {
      throw new HttpError(400, "a launch needs an adw and its values");
    }
    const catalog = await this.catalog();
    if (catalog.read_only) {
      throw new HttpError(409, "this repo's ADWs predate --describe: run `just sssf-update` to launch from the Console");
    }
    const info = catalog.adws.find((a) => a.name === req.adw);
    if (!info) throw new HttpError(404, `no ADW ${req.adw} in adws/`);
    if (!info.description) throw new HttpError(409, `${info.name} can't describe itself: ${info.error}`);
    if (info.description.resumes) {
      throw new HttpError(400, `${info.name} is a Resuming ADW: it continues an earlier Run, so it can't start a fresh one`);
    }

    const adwId = req.adw_id ?? randomBytes(4).toString("hex");
    if (!ADW_ID.test(adwId)) throw new HttpError(400, "invalid adw_id");
    if (this.entries.has(adwId) || this.hasRun(adwId)) throw new HttpError(409, `${adwId} is already a Run`);
    const argv = ["uv", "run", info.file, ...argsFor(info, info.description, req.values, adwId)];
    return { adw_id: adwId, argv, command: argv.map(shellQuote).join(" ") };
  }

  /**
   * Spawn the ADW. With the id a preview minted, only that preview's exact argv
   * runs: a form edited after its review is turned down, not launched unseen.
   */
  async start(req: LaunchRequest): Promise<Launch> {
    const preview = await this.plan(req);
    if (req.adw_id !== undefined) {
      const shown = this.previewed.get(req.adw_id);
      if (!shown || shown.join("\0") !== preview.argv.join("\0")) {
        throw new HttpError(409, "this isn't the command you reviewed: review the new one before launching it");
      }
      this.previewed.delete(req.adw_id);
    }
    mkdirSync(join(this.sessionsDir, preview.adw_id), { recursive: true });
    const logPath = this.logFile(preview.adw_id);
    const fd = openSync(logPath, "a");
    const entry: Tracked = {
      launch: { ...preview, adw: req.adw, started_at: new Date().toISOString(), exit_code: null },
      exited: false,
    };
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
      });
      child.on("error", (error) => {   // e.g. uv not on PATH: the Launch is Refused, and says why
        appendFileSync(logPath, `[console] could not start: ${error.message}\n`, "utf8");
        entry.exited = true;
      });
      child.unref();
    } finally {
      closeSync(fd);
    }
    return this.view(entry);
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

  private view(entry: Tracked): Launch {
    const { adw_id } = entry.launch;
    // The row is checked first: an ADW that wrote one and then exited is a Run, not a refusal.
    const state = this.hasRun(adw_id) ? "started" : entry.exited ? "refused" : "starting";
    const path = this.logPath(adw_id);
    return { ...entry.launch, state, log_tail: state === "started" || !path ? "" : tailOf(path) };
  }
}
