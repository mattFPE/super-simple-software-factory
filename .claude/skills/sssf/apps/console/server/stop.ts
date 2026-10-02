/**
 * Stopping a Run: the Console's Stop runs the factory's own verified kill,
 * `adws/adw_modules/procs.py stop`, exactly as `just kill` does, and reports
 * what it printed. Which pids to signal, in what order, and whether a pid
 * still runs what was recorded are all decided there, never here (ADR 0001).
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
const PREDATES_STOP = "this repo's adws/adw_modules/procs.py predates Stop: run `just sssf-update`";

export async function stopRun(repoRoot: string, dbPath: string, adwId: string): Promise<StopReport> {
  if (!ADW_ID.test(adwId)) throw new HttpError(400, "invalid adw_id");
  const proc = Bun.spawn(["uv", "run", PROCS, "stop", adwId, "--db", dbPath, "--json",
    "--grace", String(GRACE_SECONDS)], {
    cwd: repoRoot, stdout: "pipe", stderr: "pipe", windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  const timer = setTimeout(() => proc.kill(), STOP_TIMEOUT_MS);
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited,
  ]).finally(() => clearTimeout(timer));
  let printed: unknown = null;
  try { printed = JSON.parse(out); } catch { /* not a report: the error below says why */ }
  if (code === 0 && printed) return printed as StopReport;
  const refused = (printed as { error?: unknown } | null)?.error;
  if (code === 2 && typeof refused === "string") throw new HttpError(409, refused);
  // A procs.py from before Stop has no command line: it exits 0 having printed
  // nothing. A missing one fails to open, which uv reports on stderr.
  if (code === 0 || /No such file|can't open file|not found/i.test(err)) throw new HttpError(409, PREDATES_STOP);
  const said = err.trim().split(/\r?\n/).at(-1) ?? "";
  throw new HttpError(500, `procs.py stop exited ${code}${said ? `: ${said}` : ""}`);
}
