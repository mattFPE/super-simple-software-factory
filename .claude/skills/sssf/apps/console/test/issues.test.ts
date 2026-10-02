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
 *   bun test
 */
import { Database } from "bun:sqlite";
import { describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type { AdwCatalog, IssueListing, Launch, LaunchPreview } from "../shared/types.ts";
import { APP_DIR, onCleanup, port as nextPort, serve, tempDir, until } from "./support.ts";

setDefaultTimeout(60_000);

const TRACER = resolve(APP_DIR, "..", "..", "templates", "adws", "adw_modules", "tracer.py");
const ISSUE_URL = "https://github.com/acme/widgets/issues";

const OPTIONS = [
  { name: "prompt", flag: null, kind: "value", help: "what to do", default: null, choices: null, required: true },
  { name: "adw_id", flag: "--adw-id", kind: "value", help: null, default: null, choices: null, required: false },
  { name: "force", flag: "--force", kind: "flag", help: null, default: false, choices: null, required: false },
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
url = "${ISSUE_URL}/" + next(a for a in sys.argv[1:] if a.startswith("#")).lstrip("#")
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

/** A Run in the trace whose Claim phase logged this issue's URL, as issues.claim does. */
function claimedRun(r: Repo, adwId: string, number: number, status: string, startedAt: string): void {
  const db = new Database(r.db);
  db.run("INSERT INTO sessions (adw_id, adw_name, request, status, started_at) VALUES (?, 'adw_full', ?, ?, ?)",
    [adwId, `#${number}`, status, startedAt]);
  db.run("INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, status, started_at)" +
    " VALUES (?, ?, 2, 'claim', 'code', 'github', 'success', ?)", [`${adwId}_02_claim`, adwId, startedAt]);
  db.run("INSERT INTO events (event_id, adw_id, phase_id, type, name, payload_json, started_at)" +
    " VALUES (?, ?, ?, 'log', 'claim', ?, ?)",
    [`${adwId}_e1`, adwId, `${adwId}_02_claim`,
      JSON.stringify({ issue: `${ISSUE_URL}/${number}`, label: "agent-running", checklist: "none" }), startedAt]);
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
    expect(first.body.issue).toBe(4);
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
