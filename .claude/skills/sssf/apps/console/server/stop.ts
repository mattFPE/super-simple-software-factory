/**
 * Stopping a Run: the Console's Stop runs the factory's own verified kill,
 * `adws/adw_modules/procs.py stop`, exactly as `just kill` does, and reports
 * what it printed. Which pids to signal, in what order, and whether a pid
 * still runs what was recorded are all decided there, never here (ADR 0001).
 *
 * That same check answers for a Launch recovered after a restart, whose
 * process this server no longer holds: `procs.py running`.
 */
import { join } from "node:path";
import type { StopReport } from "../shared/types.ts";
import { HttpError } from "./launches.ts";

const ADW_ID = /^[A-Za-z0-9_-]{1,64}$/;
const PROCS = join("adws", "adw_modules", "procs.py");
/** How long the Run gets to settle itself (an issue comment, say) before it is killed. */
const GRACE_SECONDS = 30;
// Past the grace: the stop request's acknowledgement, and reading command lines.
const STOP_TIMEOUT_MS = (GRACE_SECONDS + 60) * 1000;
const RUNNING_TIMEOUT_MS = 120_000;   // a first `uv run` may resolve the script's deps
const PREDATES_STOP = "this repo's adws/adw_modules/procs.py predates Stop: run `just sssf-update`";

async function procs(repoRoot: string, args: string[], timeoutMs: number) {
  const proc = Bun.spawn(["uv", "run", PROCS, ...args], {
    cwd: repoRoot, stdout: "pipe", stderr: "pipe", windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  const timer = setTimeout(() => proc.kill(), timeoutMs);
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited,
  ]).finally(() => clearTimeout(timer));
  let printed: unknown = null;
  try { printed = JSON.parse(out); } catch { /* not JSON: the caller says why */ }
  const said = err.trim().split(/\r?\n/).at(-1) ?? "";
  const failed = `procs.py ${args[0]} exited ${code}${said ? `: ${said}` : ""}`;
  return { printed, err, code, failed };
}

export async function stopRun(repoRoot: string, dbPath: string, adwId: string): Promise<StopReport> {
  if (!ADW_ID.test(adwId)) throw new HttpError(400, "invalid adw_id");
  const { printed, err, code, failed } = await procs(repoRoot,
    ["stop", adwId, "--db", dbPath, "--json", "--grace", String(GRACE_SECONDS)], STOP_TIMEOUT_MS);
  if (code === 0 && printed) return printed as StopReport;
  const refused = (printed as { error?: unknown } | null)?.error;
  if (code === 2 && typeof refused === "string") throw new HttpError(409, refused);
  // A procs.py from before Stop has no command line: it exits 0 having printed
  // nothing. A missing one fails to open, which uv reports on stderr.
  if (code === 0 || /No such file|can't open file|not found/i.test(err)) throw new HttpError(409, PREDATES_STOP);
  throw new HttpError(500, failed);
}

/**
 * Whether this pid still runs the argv a Launch spawned: alive, and not a
 * recycled pid. procs.py turns the argv into the command it compares, as it
 * records one. A check that can't answer (a procs.py from before it, say)
 * throws, and the caller decides what an unknown means.
 */
export async function stillRunning(repoRoot: string, pid: number, argv: string[], adwId: string): Promise<boolean> {
  const { printed, code, failed } = await procs(repoRoot,
    ["running", String(pid), "--adw-id", adwId, "--json", "--", ...argv], RUNNING_TIMEOUT_MS);
  const running = (printed as { running?: unknown } | null)?.running;
  if (code === 0 && typeof running === "boolean") return running;
  throw new Error(failed);
}
