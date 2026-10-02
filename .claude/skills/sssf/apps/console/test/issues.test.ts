/**
 * The Ready issues panel's server side, through the HTTP API alone (#13).
 *
 * A throwaway git repo whose `adws/adw_modules/issues.py` is a stub of
 * `--list-ready --json`: it prints the repo's `listing.json` (or fails when
 * there is none) and counts its calls in `list-calls`, so a test decides what
 * GitHub said and sees how often it was asked. The stub ADWs are spawned for
 * real with `uv run`: adw_full writes its session row once the repo's `hold`
 * file goes and logs its Claim once `claim-hold` goes, so a test decides how
 * long its Launch is Starting and how long its Run goes unclaimed.
 *
 * A Local Markdown repo (#17) runs the factory's real issues.py instead,
 * against a fixture `.scratch/` tree: there is no GitHub to stub, and its
 * listing must match the terminal's.
 *
 *   bun test
 */
import { Database } from "bun:sqlite";
import { describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type { AdwCatalog, IssueListing, Launch, LaunchPreview } from "../shared/types.ts";
import { APP_DIR, onCleanup, port as nextPort, serve, tempDir, until } from "./support.ts";

setDefaultTimeout(60_000);

const MODULES = resolve(APP_DIR, "..", "..", "templates", "adws", "adw_modules");
const TRACER = join(MODULES, "tracer.py");
const ISSUE_URL = "https://github.com/acme/widgets/issues";

const OPTIONS = [
  { name: "prompt", flag: null, kind: "value", help: "what to do", default: null, choices: null, required: true },
  { name: "adw_id", flag: "--adw-id", kind: "value", help: null, default: null, choices: null, required: false },
  { name: "force", flag: "--force", kind: "flag", help: null, default: false, choices: null, required: false },
  { name: "merge", flag: "--merge", kind: "flag", help: null, default: false, choices: null, required: false },
];

function adw(doc: string, phases: string, description: object, body = ""): string {
  return `"""${doc}\n\nPhases: ${phases}\n"""
import json, os, sqlite3, sys, time
from datetime import datetime, timezone
if "--describe" in sys.argv:
    print(json.dumps(json.loads(${JSON.stringify(JSON.stringify(description))})))
    sys.exit(0)
adw_id = sys.argv[sys.argv.index("--adw-id") + 1]
${body}`;
}

const WAITS = `
def wait(name):
    end = time.time() + 60
    while os.path.exists(name) and time.time() < end:
        time.sleep(0.1)
`;

/**
 * Its row and its process once the hold goes, then its Claim once the
 * claim-hold goes, logged as issues.claim logs one. A rerun joins its Run's
 * row as session.ensure does, and claims the issue again.
 */
const CLAIMS_ON_RELEASE = `${WAITS}
wait("hold")
now = datetime.now(timezone.utc).isoformat()
with sqlite3.connect("adws/adw_data/sssf.db", timeout=10) as c:
    c.execute("INSERT INTO sessions (adw_id, adw_name, request, status, started_at) VALUES (?, 'adw_full', ?, 'running', ?)"
              " ON CONFLICT(adw_id) DO UPDATE SET status = 'running'", (adw_id, json.dumps(sys.argv[1:]), now))
    c.execute("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) VALUES (?, 'adw', '', ?, ?, ?)",
              (adw_id, os.getpid(), " ".join(sys.argv), now))
wait("claim-hold")
# An issue goes first in the argv: "#4" logs its URL, a local issue its path.
ref = sys.argv[1]
url = "${ISSUE_URL}/" + ref.lstrip("#") if ref.startswith("#") else ref
with sqlite3.connect("adws/adw_data/sssf.db", timeout=10) as c:
    c.execute("INSERT OR IGNORE INTO phases (phase_id, adw_id, seq, name, kind, owner, status, started_at)"
              " VALUES (?, ?, 2, 'claim', 'code', 'github', 'success', ?)", (adw_id + "_02_claim", adw_id, now))
    c.execute("INSERT INTO events (event_id, adw_id, phase_id, type, name, payload_json, started_at)"
              " VALUES (?, ?, ?, 'log', 'claim', ?, ?)",
              (adw_id + "_" + str(os.getpid()), adw_id, adw_id + "_02_claim", json.dumps({"issue": url}), now))
`;

const COMMITS = { commits: true, resumes: false, mutually_exclusive: [], options: OPTIONS };
const READ_ONLY = { ...COMMITS, commits: false };

const ADWS: Record<string, string> = {
  "adw_full.py": adw("ADW Full — plans, builds, tests, commits.",
    "engineer(request) -> planner -> builder\n        -> tester -> git(commit)", COMMITS, CLAIMS_ON_RELEASE),
  // Its retry loops repeat phases it already has: fewer phases than adw_full, for all their arrows.
  "adw_short.py": adw("ADW Short — builds and commits.",
    "engineer(request) -> builder -> tester [-> builder(fix) -> tester ... bounded]\n" +
    "        [-> builder(revise) -> tester ... bounded] -> git(commit)", COMMITS),
  // More phases than adw_full, but it commits nothing: never the issue default, and it claims nothing.
  "adw_look.py": adw("ADW Look — reads and reports.",
    "engineer(request) -> scout -> planner -> reviewer -> reporter -> writer", READ_ONLY, `${WAITS}\nwait("hold")\n`),
  "adw_resume.py": adw("ADW Resume — continues a Run.",
    "a -> b -> c -> d -> e -> f -> g", { ...COMMITS, resumes: true }),
};

/** What `issues.py --list-ready --json` prints: the fixture in listing.json, counting each call. */
const LIST_READY = `# a stub of: issues.py --list-ready --json
import pathlib, sys
with open("list-calls", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
listing = pathlib.Path("listing.json")
if not listing.exists():
    sys.exit("Traceback: something broke in issues.py")
print(listing.read_text())
`;

interface Repo { root: string; db: string; port: number }

function repo(listReady: string | null = LIST_READY): Repo {
  const root = tempDir();
  spawnSync("git", ["init", "-q"], { cwd: root });
  mkdirSync(join(root, "adws", "adw_data"), { recursive: true });
  mkdirSync(join(root, "adws", "adw_modules"), { recursive: true });
  for (const [name, source] of Object.entries(ADWS)) writeFileSync(join(root, "adws", name), source, "utf8");
  if (listReady !== null) writeFileSync(join(root, "adws", "adw_modules", "issues.py"), listReady, "utf8");
  const schema = /SCHEMA = """([\s\S]*?)"""/.exec(readFileSync(TRACER, "utf8"))![1]!;
  const path = join(root, "adws", "adw_data", "sssf.db");
  const db = new Database(path);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec(schema);
  db.close();
  for (const hold of ["hold", "claim-hold"]) {
    writeFileSync(join(root, hold), "", "utf8");
    onCleanup(() => rmSync(join(root, hold), { force: true }));
  }
  return { root, db: path, port: nextPort() };
}

interface Listed { number: number; verdict: string; why?: string | null; run_instead?: object[]; prs?: string[] }

function listing(r: Repo, issues: Listed[], extra: object = {}): void {
  const full = issues.map((i) => ({
    title: `Issue ${i.number}`, url: `${ISSUE_URL}/${i.number}`, why: null,
    blocked_by: [], tickets: [], run_instead: [], prs: [], ...i,
  }));
  writeFileSync(join(r.root, "listing.json"), JSON.stringify({
    tracker: "GitHub", repo: "acme/widgets", ready_label: "ready-for-agent", available: true,
    unavailable: null, issues: full, truncated: false, ...extra,
  }), "utf8");
}

function listCalls(r: Repo): number {
  const path = join(r.root, "list-calls");
  return existsSync(path) ? readFileSync(path, "utf8").split("\n").filter(Boolean).length : 0;
}

/**
 * A Run in the trace whose Claim phase logged this issue as issues.claim does:
 * a GitHub issue by its URL, a local one (a path) by its path.
 */
function claimedRun(r: Repo, adwId: string, issue: number | string, status: string, startedAt: string): void {
  const local = typeof issue === "string";
  const db = new Database(r.db);
  db.run("INSERT INTO sessions (adw_id, adw_name, request, status, started_at) VALUES (?, 'adw_full', ?, ?, ?)",
    [adwId, local ? issue : `#${issue}`, status, startedAt]);
  db.run("INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, status, started_at)" +
    " VALUES (?, ?, 2, 'claim', 'code', 'github', 'success', ?)", [`${adwId}_02_claim`, adwId, startedAt]);
  db.run("INSERT INTO events (event_id, adw_id, phase_id, type, name, payload_json, started_at)" +
    " VALUES (?, ?, ?, 'log', 'claim', ?, ?)",
    [`${adwId}_e1`, adwId, `${adwId}_02_claim`,
      JSON.stringify(local ? { issue, status: "agent-running", checklist: "none" }
        : { issue: `${ISSUE_URL}/${issue}`, label: "agent-running", checklist: "none" }), startedAt]);
  db.close();
}

/** A phase of a Run in the trace that logged this payload, as worktree.enter and worktree.land log theirs. */
function phaseLog(r: Repo, adwId: string, seq: number, name: string, payload: object): void {
  const db = new Database(r.db);
  db.run("INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, status, started_at)" +
    " VALUES (?, ?, ?, ?, 'code', 'git', 'success', ?)", [`${adwId}_0${seq}_${name}`, adwId, seq, name, "2026-10-01T10:00:00+00:00"]);
  db.run("INSERT INTO events (event_id, adw_id, phase_id, type, name, payload_json, started_at)" +
    " VALUES (?, ?, ?, 'log', ?, ?, ?)",
    [`${adwId}_${name}_log`, adwId, `${adwId}_0${seq}_${name}`, name, JSON.stringify(payload), "2026-10-01T10:00:00+00:00"]);
  db.close();
}

/** A Run that worked in its own worktree, logged as worktree.enter logs it; kept on disk unless `kept` is false. */
function worktreeOf(r: Repo, adwId: string, kept = true): string {
  const path = join(`${r.root}.sssf-worktrees`, adwId);
  if (kept) {
    mkdirSync(path, { recursive: true });
    onCleanup(() => rmSync(`${r.root}.sssf-worktrees`, { recursive: true, force: true }));
  }
  phaseLog(r, adwId, 3, "worktree", { path, branch: `sssf/${adwId}`, base: "main @ 0000000", reused: false });
  return path;
}

const startConsole = (r: Repo) => serve([join("server", "index.ts"), "--db", r.db], r.port);

async function api<T>(r: Repo, path: string, body?: unknown): Promise<{ status: number; body: T }> {
  const res = await fetch(`http://127.0.0.1:${r.port}${path}`, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return { status: res.status, body: (await res.json()) as T };
}

const issues = (r: Repo) => api<IssueListing>(r, "/api/issues");

describe("the Ready issues", () => {
  test("are listed with the Runnable ones first, and the rest keep their verdict and reason", async () => {
    const r = repo();
    listing(r, [
      { number: 3, verdict: "blocked", why: "#3 is blocked by 1 open issue(s)" },
      { number: 4, verdict: "runnable" },
      { number: 5, verdict: "spec", why: "#5 is a spec split into 2 ticket(s)", run_instead: [{ number: 6 }] },
      { number: 6, verdict: "runnable" },
      { number: 7, verdict: "open_pr", why: "an open PR already closes #7" },
    ]);
    await startConsole(r);
    const { status, body } = await issues(r);
    expect(status).toBe(200);
    expect(body.available).toBe(true);
    expect(body.issues.map((i) => [i.number, i.verdict])).toEqual(
      [[4, "runnable"], [6, "runnable"], [3, "blocked"], [5, "spec"], [7, "open_pr"]]);
    expect(body.issues.find((i) => i.number === 3)!.why).toBe("#3 is blocked by 1 open issue(s)");
    expect(body.issues.find((i) => i.number === 5)!.run_instead.map((t) => t.number)).toEqual([6]);
  });

  test("are asked for once per request, and never by the server on its own", async () => {
    const r = repo();
    listing(r, [{ number: 4, verdict: "runnable" }]);
    await startConsole(r);
    await Bun.sleep(1500);
    expect(listCalls(r)).toBe(0);
    await issues(r);
    await issues(r);
    expect(listCalls(r)).toBe(2);
    expect(readFileSync(join(r.root, "list-calls"), "utf8")).toContain("--list-ready --json");
  });

  test("say so when the list was truncated", async () => {
    const r = repo();
    listing(r, [{ number: 4, verdict: "runnable" }], { truncated: true });
    await startConsole(r);
    expect((await issues(r)).body.truncated).toBe(true);
  });

  test("link a claimed issue to the most recent Run whose Claim logged it", async () => {
    const r = repo();
    listing(r, [{ number: 8, verdict: "claimed", why: "#8 is labelled `agent-running`" },
      { number: 9, verdict: "runnable" }]);
    claimedRun(r, "old0run0", 8, "fail", "2026-10-01T10:00:00+00:00");
    claimedRun(r, "new0run0", 8, "running", "2026-10-02T10:00:00+00:00");
    await startConsole(r);
    const { body } = await issues(r);
    expect(body.issues.find((i) => i.number === 8)!.run).toEqual(
      { adw_id: "new0run0", adw_name: "adw_full", status: "running", worktree: null, pr: null });
    expect(body.issues.find((i) => i.number === 9)!.run).toBeNull();
  });

  test("that aren't available say why in one line, and a typed-prompt Launch still works", async () => {
    const r = repo();
    writeFileSync(join(r.root, "listing.json"), JSON.stringify({
      tracker: "GitHub", repo: null, ready_label: "ready-for-agent", available: false,
      unavailable: { reason: "the GitHub CLI (`gh`) is not on PATH", fix: "Install it." },
      issues: [], truncated: false }), "utf8");
    await startConsole(r);
    const { status, body } = await issues(r);
    expect(status).toBe(200);
    expect(body.available).toBe(false);
    expect(body.unavailable).toEqual({ reason: "the GitHub CLI (`gh`) is not on PATH", fix: "Install it." });
    expect((await api(r, "/api/launches", { adw: "adw_short", values: { prompt: "typed" } })).status).toBe(201);
  });

  test("that issues.py can't list are unavailable, with what it said", async () => {
    const r = repo();   // no listing.json: the stub fails
    await startConsole(r);
    const { status, body } = await issues(r);
    expect(status).toBe(200);
    expect(body.available).toBe(false);
    expect(body.unavailable!.reason).toContain("something broke in issues.py");
  });

  test("in a repo whose issues.py predates --list-ready are unavailable until it updates", async () => {
    const r = repo(`"""A GitHub issue as a run's request."""\nfrom . import git_helper\n`);
    await startConsole(r);
    const { body } = await issues(r);
    expect(body.available).toBe(false);
    expect(body.unavailable!.fix).toContain("just sssf-update");
  });
});

describe("a Launch from an issue", () => {
  test("defaults to the committing, non-resuming ADW with the most phases", async () => {
    const r = repo();
    await startConsole(r);
    expect((await api<AdwCatalog>(r, "/api/adws")).body.default_issue_adw).toBe("adw_full");
  });

  test("holds its issue until the Run's Claim lands, turning down a second Launch of it", async () => {
    const r = repo();
    listing(r, [{ number: 4, verdict: "runnable" }, { number: 6, verdict: "runnable" }]);
    await startConsole(r);
    const first = await api<Launch>(r, "/api/launches", { adw: "adw_full", values: { prompt: "#4" } });
    expect(first.status).toBe(201);
    expect(first.body.issue).toBe("#4");
    expect(first.body.holds_issue).toBe(true);
    const heldBy = async (n: number) => (await issues(r)).body.issues.find((i) => i.number === n)!.held_by;
    const previewOf = (prompt: string) =>
      api<{ error: string }>(r, "/api/launches/preview", { adw: "adw_short", values: { prompt } });

    expect(await heldBy(4)).toBe(first.body.adw_id);
    expect(await heldBy(6)).toBeNull();
    for (const prompt of ["#4", `${ISSUE_URL}/4`]) {
      const again = await previewOf(prompt);
      expect(again.status).toBe(409);
      expect(again.body.error).toContain(first.body.adw_id);
    }
    expect((await previewOf("#6")).status).toBe(200);

    // Started but not yet claimed: GitHub would still call it Runnable.
    rmSync(join(r.root, "hold"), { force: true });
    expect(await until(async () =>
      (await api<Launch[]>(r, "/api/launches")).body.find((l) => l.adw_id === first.body.adw_id)?.state === "started",
    30)).toBe(true);
    expect(await heldBy(4)).toBe(first.body.adw_id);
    expect((await previewOf("#4")).status).toBe(409);

    rmSync(join(r.root, "claim-hold"), { force: true });
    expect(await until(async () => (await heldBy(4)) === null, 30)).toBe(true);
    const launched = (await api<Launch[]>(r, "/api/launches")).body.find((l) => l.adw_id === first.body.adw_id)!;
    expect(launched.holds_issue).toBe(false);
    expect((await previewOf("#4")).status).toBe(200);
  });

  test("by an ADW that commits nothing claims nothing, so it holds nothing", async () => {
    const r = repo();
    listing(r, [{ number: 4, verdict: "runnable" }]);
    await startConsole(r);
    const look = await api<Launch>(r, "/api/launches", { adw: "adw_look", values: { prompt: "#4" } });
    expect(look.body.state).toBe("starting");
    expect(look.body.holds_issue).toBe(false);
    expect((await issues(r)).body.issues[0]!.held_by).toBeNull();
    expect((await api(r, "/api/launches/preview", { adw: "adw_full", values: { prompt: "#4" } })).status).toBe(200);
  });
});

/** The rerun line of a failed Run's outcome comment, as issues._outcome writes it. */
const outcomeRerun = (name: string, number: number, adwId: string) => `uv run adws/${name}.py "#${number}" --adw-id ${adwId}`;
const outcomeForce = (name: string, number: number) => `uv run adws/${name}.py "#${number}" --force`;

describe("an issue whose latest Run failed", () => {
  test("with its worktree kept offers Rerun with that Run's ADW and adw_id, beside a fresh Launch", async () => {
    const r = repo();
    listing(r, [{ number: 8, verdict: "runnable" }, { number: 9, verdict: "runnable" }]);
    claimedRun(r, "fail0run", 8, "fail", "2026-10-01T10:00:00+00:00");
    worktreeOf(r, "fail0run");
    await startConsole(r);
    const { body } = await issues(r);
    const eight = body.issues.find((i) => i.number === 8)!;
    expect(eight.verdict).toBe("runnable");   // a fresh Launch is still there
    expect(eight.rerun).toEqual({ adw: "adw_full", adw_id: "fail0run", force: false, pr: null });
    expect(body.issues.find((i) => i.number === 9)!.rerun).toBeNull();
  });

  test("offers no Rerun once its worktree is gone, or when a later Run of it succeeded", async () => {
    const r = repo();
    listing(r, [{ number: 8, verdict: "runnable" }, { number: 9, verdict: "runnable" }]);
    claimedRun(r, "gone0run", 8, "fail", "2026-10-01T10:00:00+00:00");
    worktreeOf(r, "gone0run", false);
    claimedRun(r, "fail0run", 9, "fail", "2026-10-01T10:00:00+00:00");
    worktreeOf(r, "fail0run");
    claimedRun(r, "good0run", 9, "success", "2026-10-02T10:00:00+00:00");
    await startConsole(r);
    const { body } = await issues(r);
    expect(body.issues.find((i) => i.number === 8)!.rerun).toBeNull();
    expect(body.issues.find((i) => i.number === 9)!.rerun).toBeNull();
  });

  test("that left an open PR offers Rerun with --force, linking the PR", async () => {
    const r = repo();
    const pr = "https://github.com/acme/widgets/pull/31";
    listing(r, [{ number: 7, verdict: "open_pr", why: `an open PR already closes #7: ${pr}`, prs: [pr] }]);
    claimedRun(r, "pr00run0", 7, "fail", "2026-10-01T10:00:00+00:00");
    phaseLog(r, "pr00run0", 9, "land", { pr, pr_repo: "acme/widgets" });
    await startConsole(r);
    const seven = (await issues(r)).body.issues[0]!;
    expect(seven.rerun).toEqual({ adw: "adw_full", adw_id: null, force: true, pr });

    const preview = await api<LaunchPreview>(r, "/api/launches/preview",
      { adw: "adw_full", values: { prompt: "#7", force: true } });
    expect(preview.status).toBe(200);
    expect(preview.body.adw_id).not.toBe("pr00run0");
    // The Console mints every Run's id, so it follows the outcome comment's command.
    expect(preview.body.command).toBe(`${outcomeForce("adw_full", 7)} --adw-id ${preview.body.adw_id}`);

    const started = await api<Launch>(r, "/api/launches",
      { adw: "adw_full", values: { prompt: "#7", force: true }, adw_id: preview.body.adw_id });
    expect(started.status).toBe(201);
    expect(started.body.argv).toEqual(preview.body.argv);
    expect(started.body.rerun).toBe(false);   // a fresh Run, not a join
    expect(started.body.holds_issue).toBe(true);
  });

  test("reviews the exact rerun command of its outcome comment, then reruns under the same adw_id", async () => {
    const r = repo();
    listing(r, [{ number: 8, verdict: "runnable" }]);
    claimedRun(r, "fail0run", 8, "fail", "2026-10-01T10:00:00+00:00");
    worktreeOf(r, "fail0run");
    await startConsole(r);
    const rerun = { adw: "adw_full", values: { prompt: "#8" }, reruns: "fail0run" };

    const preview = await api<LaunchPreview>(r, "/api/launches/preview", rerun);
    expect(preview.status).toBe(200);
    expect(preview.body.adw_id).toBe("fail0run");
    expect(preview.body.command).toBe(outcomeRerun("adw_full", 8, "fail0run"));

    const started = await api<Launch>(r, "/api/launches", rerun);
    expect(started.status).toBe(201);
    expect(started.body.adw_id).toBe("fail0run");
    expect(started.body.rerun).toBe(true);
    // The Run is already in the trace: the rerun is Starting until its own process joins it.
    expect(started.body.state).toBe("starting");
    expect(started.body.holds_issue).toBe(true);

    const launch = async () => (await api<Launch[]>(r, "/api/launches")).body.find((l) => l.adw_id === "fail0run")!;
    rmSync(join(r.root, "hold"), { force: true });
    expect(await until(async () => (await launch()).state === "started", 30)).toBe(true);
    // The old Run's Claim is in the trace already; the rerun holds the issue until its own lands.
    expect((await launch()).holds_issue).toBe(true);
    rmSync(join(r.root, "claim-hold"), { force: true });
    expect(await until(async () => !(await launch()).holds_issue, 30)).toBe(true);
  });

  test("is rerun only with the ADW that ran it, and only once it has settled", async () => {
    const r = repo();
    claimedRun(r, "fail0run", 8, "fail", "2026-10-01T10:00:00+00:00");
    claimedRun(r, "busy0run", 9, "running", "2026-10-01T10:00:00+00:00");
    await startConsole(r);
    const preview = (body: object) => api<{ error: string }>(r, "/api/launches/preview", body);

    const other = await preview({ adw: "adw_short", values: { prompt: "#8" }, reruns: "fail0run" });
    expect(other.status).toBe(400);
    expect(other.body.error).toContain("adw_full");
    expect((await preview({ adw: "adw_full", values: { prompt: "#9" }, reruns: "busy0run" })).status).toBe(409);
    expect((await preview({ adw: "adw_full", values: { prompt: "#8" }, reruns: "nope0run" })).status).toBe(404);
    expect((await preview({ adw: "adw_resume", values: { prompt: "#8" }, reruns: "fail0run" })).status).toBe(400);
    expect((await preview({ adw: "adw_full", values: { prompt: "#8" }, reruns: "fail0run", adw_id: "x" })).status)
      .toBe(400);
  });

  test("launches only the rerun command its preview showed", async () => {
    const r = repo();
    claimedRun(r, "fail0run", 8, "fail", "2026-10-01T10:00:00+00:00");
    await startConsole(r);
    const unseen = await api<{ error: string }>(r, "/api/launches",
      { adw: "adw_full", values: { prompt: "#8" }, reruns: "fail0run" });
    expect(unseen.status).toBe(409);
    expect(unseen.body.error).toContain("review");
  });
});

/** A local Ticket, as /to-tickets' local template writes one. */
function ticket(number: number, title: string, status: string, blockedBy?: string): string {
  return [`# ${String(number).padStart(2, "0")} — ${title}`, "", `**What to build:** ${title.toLowerCase()}.`, "",
    ...(blockedBy ? [`**Blocked by:** ${blockedBy}`, ""] : []), `**Status:** ${status}`, "",
    `- [ ] ${title} works`, ""].join("\n");
}

/** A local Spec. */
function spec(title: string, status = "ready-for-agent"): string {
  return `# ${title}\n\nStatus: ${status}\n\n## Problem Statement\n\nPeople need ${title.toLowerCase()}.\n`;
}

const WIDGETS = ".scratch/widgets";
const PARTS = `${WIDGETS}/issues/01-make-the-parts.md`;
const ASSEMBLE = `${WIDGETS}/issues/02-assemble-them.md`;
const PAINT = `${WIDGETS}/issues/03-paint-it.md`;
const BOLTS = ".scratch/gizmos/issues/01-tighten-the-bolts.md";   // a second feature's Ticket 01
const GADGETS = ".scratch/gadgets/spec.md";                       // a Spec with no Tickets: its own Ticket

/**
 * A Local Markdown repo listed by the factory's real issues.py, not a stub:
 * a Spec split into Tickets — one Runnable, one blocked by it, one claimed —
 * a second feature numbering its Tickets from 01 too, and an unsplit Spec.
 */
function localRepo(): Repo {
  const r = repo(null);
  cpSync(MODULES, join(r.root, "adws", "adw_modules"), { recursive: true });
  const write = (path: string, text: string) => {
    mkdirSync(join(r.root, path, ".."), { recursive: true });
    writeFileSync(join(r.root, path), text, "utf8");
  };
  write("docs/agents/issue-tracker.md", "# Issue tracker: Local Markdown\n\nIssues live under `.scratch/`.\n");
  write(`${WIDGETS}/spec.md`, spec("Widgets"));
  write(PARTS, ticket(1, "Make the parts", "ready-for-agent"));
  write(ASSEMBLE, ticket(2, "Assemble them", "ready-for-agent", "01"));
  write(PAINT, ticket(3, "Paint it", "agent-running"));
  write(".scratch/gizmos/spec.md", spec("Gizmos", "needs-triage"));
  write(BOLTS, ticket(1, "Tighten the bolts", "ready-for-agent"));
  write(GADGETS, spec("Gadgets"));
  return r;
}

/** What the terminal says: the same issues.py --list-ready --json the Console runs. */
function fromTerminal(r: Repo): IssueListing {
  const done = spawnSync("uv", ["run", "adws/adw_modules/issues.py", "--list-ready", "--json"],
    { cwd: r.root, encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" } });
  expect(done.status).toBe(0);
  return JSON.parse(done.stdout) as IssueListing;
}

const byPath = (listed: IssueListing, path: string) => listed.issues.find((i) => i.path === path)!;

describe("a Local Markdown repo's Ready issues", () => {
  test("are listed with the verdicts and reasons the terminal gives, Runnable ones first", async () => {
    const r = localRepo();
    await startConsole(r);
    const { status, body } = await issues(r);
    expect(status).toBe(200);
    expect(body.available).toBe(true);
    expect(body.tracker).toBe("Local Markdown");
    expect(body.issues.map((i) => [i.path, i.verdict])).toEqual([
      [GADGETS, "runnable"], [BOLTS, "runnable"], [PARTS, "runnable"],
      [ASSEMBLE, "blocked"], [PAINT, "claimed"], [`${WIDGETS}/spec.md`, "spec"],
    ]);
    const terminal = fromTerminal(r);
    expect(terminal.issues.length).toBe(body.issues.length);
    for (const issue of terminal.issues) {
      const listed = byPath(body, issue.path!);
      expect([listed.verdict, listed.why, listed.number, listed.url])
        .toEqual([issue.verdict, issue.why, issue.number, issue.url]);
    }
    expect(byPath(body, GADGETS).number).toBeNull();
    expect(byPath(body, ASSEMBLE).blocked_by.map((b) => b.path)).toEqual([PARTS]);
    // A Spec links to its Tickets by path: two features can each have a Ticket 01.
    expect(byPath(body, `${WIDGETS}/spec.md`).run_instead.map((t) => t.path)).toEqual([PARTS]);
  });

  test("link a claimed Ticket to the Run whose Claim logged its path", async () => {
    const r = localRepo();
    claimedRun(r, "pnt0run0", PAINT, "running", "2026-10-02T10:00:00+00:00");
    await startConsole(r);
    const { body } = await issues(r);
    expect(byPath(body, PAINT).run).toEqual(
      { adw_id: "pnt0run0", adw_name: "adw_full", status: "running", worktree: null, pr: null });
    expect(byPath(body, PARTS).run).toBeNull();
  });
});

describe("a Launch from a local Ticket", () => {
  test("passes its path as the issue, landing with --merge, and holds that Ticket alone until its Claim lands", async () => {
    const r = localRepo();
    await startConsole(r);
    await issues(r);   // as the Launch pane does on open
    const pick = { adw: "adw_full", values: { prompt: PARTS, merge: true } };
    const preview = await api<LaunchPreview>(r, "/api/launches/preview", pick);
    expect(preview.status).toBe(200);
    expect(preview.body.command).toBe(`uv run adws/adw_full.py ${PARTS} --merge --adw-id ${preview.body.adw_id}`);

    const started = await api<Launch>(r, "/api/launches", { ...pick, adw_id: preview.body.adw_id });
    expect(started.status).toBe(201);
    expect(started.body.issue).toBe(PARTS);
    expect(started.body.holds_issue).toBe(true);
    const listed = (await issues(r)).body;
    expect(byPath(listed, PARTS).held_by).toBe(started.body.adw_id);
    expect(byPath(listed, BOLTS).held_by).toBeNull();   // the other Ticket 01

    const previewOf = (prompt: string) =>
      api<{ error: string }>(r, "/api/launches/preview", { adw: "adw_short", values: { prompt } });
    for (const prompt of [PARTS, `./${PARTS}`, PARTS.replaceAll("/", "\\")]) {
      const again = await previewOf(prompt);
      expect(again.status).toBe(409);
      expect(again.body.error).toContain(PARTS);
    }
    expect((await previewOf(BOLTS)).status).toBe(200);

    for (const hold of ["hold", "claim-hold"]) rmSync(join(r.root, hold), { force: true });
    expect(await until(async () => byPath((await issues(r)).body, PARTS).held_by === null, 30)).toBe(true);
  });

  test("is one even before the Console has listed the issues, while a GitHub repo's same path is a request file", async () => {
    const r = localRepo();
    await startConsole(r);
    const first = await api<Launch>(r, "/api/launches", { adw: "adw_full", values: { prompt: PARTS } });
    expect(first.status).toBe(201);
    expect(first.body.argv.slice(3)).toEqual([PARTS, "--adw-id", first.body.adw_id]);
    expect(first.body.holds_issue).toBe(true);

    const github = repo();
    await startConsole(github);
    const plain = await api<Launch>(github, "/api/launches", { adw: "adw_full", values: { prompt: PARTS } });
    expect(plain.status).toBe(201);
    expect(plain.body.issue).toBeNull();
    expect(plain.body.argv.slice(3)).toEqual(["--adw-id", plain.body.adw_id, "--", PARTS]);
  });
});

describe("a local Ticket whose latest Run failed", () => {
  test("with its worktree kept offers Rerun, running its outcome comment's command under the same adw_id", async () => {
    const r = localRepo();
    claimedRun(r, "fail0run", PARTS, "fail", "2026-10-01T10:00:00+00:00");
    worktreeOf(r, "fail0run");
    await startConsole(r);
    const parts = byPath((await issues(r)).body, PARTS);
    expect(parts.verdict).toBe("runnable");
    expect(parts.rerun).toEqual({ adw: "adw_full", adw_id: "fail0run", force: false, pr: null });

    const rerun = { adw: "adw_full", values: { prompt: PARTS }, reruns: "fail0run" };
    const preview = await api<LaunchPreview>(r, "/api/launches/preview", rerun);
    expect(preview.status).toBe(200);
    // issues._outcome's `uv run adws/adw_full.py "<path>" --adw-id fail0run`, as a shell splits it.
    expect(preview.body.argv).toEqual(["uv", "run", "adws/adw_full.py", PARTS, "--adw-id", "fail0run"]);
    const started = await api<Launch>(r, "/api/launches", rerun);
    expect(started.status).toBe(201);
    expect(started.body.rerun).toBe(true);
    expect(started.body.holds_issue).toBe(true);
  });
});
