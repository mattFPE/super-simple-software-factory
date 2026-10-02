/**
 * The repo's Ready issues: `adws/adw_modules/issues.py --list-ready --json`,
 * the factory's own listing, each issue with the verdict a Launch would reach.
 * Which issues may run is decided there, never here, and the Console never
 * asks GitHub itself (ADR 0001). It runs only when the UI asks — on open, on
 * refresh, once a Launch's Claim lands — so nothing here polls GitHub.
 *
 * What the Console adds is its own: Runnable issues first, the Run whose
 * Claim logged each issue (from the trace), a Launch of it whose Claim
 * hasn't landed yet, and the Rerun its failed Run's outcome comment offers.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { IssueListing, IssueRerun, IssueRun, ReadyIssue } from "../shared/types.ts";
import { claimingAdw, issueRef } from "../shared/issues.ts";

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
  /** By issue reference: `#42`, or a local issue's path. */
  heldIssues(): Map<string, string>;
}

function unavailable(why: NonNullable<IssueListing["unavailable"]>): IssueListing {
  return { tracker: null, repo: null, ready_label: null, available: false,
    unavailable: why, issues: [], truncated: false };
}

/**
 * The Rerun a failed (or stopped) Run's outcome comment tells the engineer to
 * type (issues._outcome). One that left an open PR is started afresh with
 * `--force`, since that PR is why a plain Launch would refuse; one that kept
 * its worktree reruns under its own adw_id to pick it back up. Whether either
 * may run is still the verdict's to say: Python's, never this.
 */
function rerunOf(issue: Omit<ReadyIssue, "run" | "rerun" | "held_by">, run: IssueRun | null): IssueRerun | null {
  const adw = claimingAdw(run?.adw_name);
  if (!run || !adw || (run.status !== "fail" && run.status !== "stopped")) return null;
  if (run.pr && issue.verdict === "open_pr" && issue.prs.includes(run.pr)) {
    return { adw, adw_id: null, force: true, pr: run.pr };
  }
  if (run.worktree && issue.verdict === "runnable") return { adw, adw_id: run.adw_id, force: false, pr: null };
  return null;
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
  const issues: ReadyIssue[] = listing.issues.map((issue) => {
    const logged = runs.get(issue.url);
    // A worktree counts only while it is still on disk: one removed by hand can't be picked back up.
    const run = logged ? { ...logged, worktree: logged.worktree && existsSync(logged.worktree) ? logged.worktree : null } : null;
    return { ...issue, run, rerun: rerunOf(issue, run), held_by: held.get(issueRef(issue)) ?? null };
  });
  // A stable sort: within each half, the order the Tracker gave.
  issues.sort((a, b) => Number(b.verdict === "runnable") - Number(a.verdict === "runnable"));
  return { ...listing, issues };
}
