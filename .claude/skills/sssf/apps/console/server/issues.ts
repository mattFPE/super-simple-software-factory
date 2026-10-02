/**
 * The repo's Ready issues: `adws/adw_modules/issues.py --list-ready --json`,
 * the factory's own listing, each issue with the verdict a Launch would reach.
 * Which issues may run is decided there, never here, and the Console never
 * asks GitHub itself (ADR 0001). It runs only when the UI asks — on open, on
 * refresh, once a Launch's Claim lands — so nothing here polls GitHub.
 *
 * What the Console adds is its own: Runnable issues first, the Run whose
 * Claim logged each issue (from the trace), and a Launch of it whose Claim
 * hasn't landed yet.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { IssueListing, IssueRun, ReadyIssue } from "../shared/types.ts";

const ISSUES = "adws/adw_modules/issues.py";
// Past a first `uv run` resolving deps: one page of issues plus a read per issue.
const LIST_TIMEOUT_MS = 120_000;
const PREDATES = {
  reason: `this repo's ${ISSUES} predates --list-ready`,
  fix: "Run `just sssf-update` to list Ready issues here.",
};

/** What the listing needs from the rest of the server. */
export interface IssueContext {
  claimedRuns(): Map<string, IssueRun>;
  heldIssues(): Map<number, string>;
}

function unavailable(why: NonNullable<IssueListing["unavailable"]>): IssueListing {
  return { tracker: null, repo: null, ready_label: null, available: false,
    unavailable: why, issues: [], truncated: false };
}

export async function listReady(repoRoot: string, context: IssueContext): Promise<IssueListing> {
  const script = join(repoRoot, ISSUES);
  if (!existsSync(script) || !readFileSync(script, "utf8").includes("--list-ready")) {
    return unavailable(PREDATES);
  }
  const proc = Bun.spawn(["uv", "run", ISSUES, "--list-ready", "--json"], {
    cwd: repoRoot, stdout: "pipe", stderr: "pipe", windowsHide: true,
    env: { ...process.env, PYTHONUTF8: "1" },
  });
  const timer = setTimeout(() => proc.kill(), LIST_TIMEOUT_MS);
  const [out, err, code] = await Promise.all([
    new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited,
  ]).finally(() => clearTimeout(timer));

  let listing: IssueListing | null = null;
  try { listing = JSON.parse(out) as IssueListing; } catch { /* not a listing: what it said is below */ }
  if (code !== 0 || !listing || !Array.isArray(listing.issues)) {
    const said = err.trim().split(/\r?\n/).at(-1) || `exited ${code}`;
    return unavailable({
      reason: `issues.py --list-ready failed: ${said}`,
      fix: `Run \`uv run ${ISSUES} --list-ready\` in the repo to see the whole error.`,
    });
  }

  const runs = context.claimedRuns();
  const held = context.heldIssues();
  const issues: ReadyIssue[] = listing.issues.map((issue) => ({
    ...issue,
    run: runs.get(issue.url) ?? null,
    held_by: held.get(issue.number) ?? null,
  }));
  // A stable sort: within each half, the order GitHub gave.
  issues.sort((a, b) => Number(b.verdict === "runnable") - Number(a.verdict === "runnable"));
  return { ...listing, issues };
}
