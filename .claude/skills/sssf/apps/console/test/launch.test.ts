/**
 * Launching a Run from a typed prompt, through the server's HTTP API alone (#9).
 *
 * A throwaway git repo whose adws/ holds stub ADWs, each spawned for real with
 * `uv run`: adw_ok writes its session row (its request is the argv it was
 * given) and then waits, adw_refuse exits before writing any, adw_hang never
 * writes one. The stubs that wait keep going while the repo's `hold` file
 * exists, so a test decides when they finish.
 *
 *   bun test
 */
import { Database } from "bun:sqlite";
import { describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { networkInterfaces } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { AdwCatalog, HealthResponse, Launch, LaunchPreview, SessionDetail } from "../shared/types.ts";
import { APP_DIR, health, onCleanup, port as nextPort, serve, stop, tempDir, until } from "./support.ts";

setDefaultTimeout(60_000);

const TRACER = resolve(APP_DIR, "..", "..", "templates", "adws", "adw_modules", "tracer.py");
const PROMPT = `it's "#42" — $(rm -rf ~) & echo pwned | x; \`y\` %PATH% ^& \\"\nsecond line\n`;

const SHARED = [
  { name: "prompt", flag: null, kind: "value", help: "what to do", default: null, choices: null, required: true },
  { name: "config", flag: "--config", kind: "value", help: null,
    default: "adws/adw_sssf_config/sssf.config.yaml", choices: null, required: false },
  { name: "adw_id", flag: "--adw-id", kind: "value", help: "join a Run", default: null, choices: null, required: false },
];
const LANDING = ["branch", "merge", "pr"].map((name) => ({
  name, flag: `--${name}`, kind: "flag", help: `land with --${name}`, default: false, choices: null, required: false,
}));

const DESCRIBE_OK = {
  commits: true, resumes: false, mutually_exclusive: [["--branch", "--merge", "--pr"]],
  options: [...SHARED, ...LANDING,
    { name: "depth", flag: "--depth", kind: "choice", help: null, default: "shallow",
      choices: ["shallow", "deep"], required: false },
    { name: "mood", flag: "--mood", kind: "vibe", help: "a kind the Console has never seen",
      default: null, choices: null, required: false }],
};
const DESCRIBE_PLAIN = { commits: false, resumes: false, mutually_exclusive: [], options: SHARED };

/** The stubs' shared head: --describe, the minted id, the row, the hold. */
const COMMON = `
import json, os, sqlite3, sys, time
from datetime import datetime, timezone

def describe(d):
    if "--describe" in sys.argv:
        print(json.dumps(d))
        sys.exit(0)

def adw_id():
    return sys.argv[sys.argv.index("--adw-id") + 1]

def held(name="hold"):
    end = time.time() + 60
    while os.path.exists(name) and time.time() < end:
        time.sleep(0.1)

def db():
    return sqlite3.connect("adws/adw_data/sssf.db", timeout=10)
`;

function stub(doc: string, describe: object | null, body: string): string {
  const head = describe ? `describe(json.loads(${JSON.stringify(JSON.stringify(describe))}))\n` : "";
  return `"""${doc}"""\n${COMMON}\n${head}${body}`;
}

const STUBS: Record<string, string> = {
  "adw_ok.py": stub(
    "ADW OK — writes its session row, then waits for the hold.\n\n" +
      "Phases: engineer(request) -> builder\n        -> git(commit)\n",
    DESCRIBE_OK,
    `
print("starting", adw_id(), flush=True)
with db() as c:
    c.execute("INSERT INTO sessions (adw_id, adw_name, request, status, engineer, started_at)"
              " VALUES (?, 'adw_ok', ?, 'running', 'stub', ?)",
              (adw_id(), json.dumps(sys.argv[1:]), datetime.now(timezone.utc).isoformat()))
held()
with db() as c:
    c.execute("UPDATE sessions SET status = 'success' WHERE adw_id = ?", (adw_id(),))
`),
  "adw_refuse.py": stub(
    "ADW Refuse — turns every request down before it starts.\n\nPhases: engineer(request) -> builder\n",
    DESCRIBE_PLAIN,
    `
for n in range(1, 41):
    print(f"preflight line {n}")
print("agents.validate: no 'builder' agent in sssf.config.yaml", file=sys.stderr)
sys.exit(3)
`),
  "adw_hang.py": stub(
    "ADW Hang — never gets as far as a session row.\n\nPhases: engineer(request) -> scout\n",
    DESCRIBE_PLAIN,
    `
print("resolving dependencies...", flush=True)
held()
`),
  "adw_resume.py": stub(
    "ADW Resume — continues an earlier Run.\n\nPhases: builder\n",
    { ...DESCRIBE_PLAIN, resumes: true },
    `
if "refuse me" in sys.argv:
    print("no plan to build under", adw_id(), file=sys.stderr)
    sys.exit(3)
print("resuming", adw_id(), flush=True)
held()
# What session.ensure does when it joins a Run: its name and its process, then its work.
now = datetime.now(timezone.utc).isoformat()
with db() as c:
    c.execute("UPDATE sessions SET adw_name = adw_name || ' + adw_resume', status = 'running' WHERE adw_id = ?",
              (adw_id(),))
    c.execute("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) VALUES (?, 'adw', '', ?, ?, ?)",
              (adw_id(), os.getpid(), " ".join(sys.argv), now))
    c.execute("INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, status, started_at)"
              " VALUES (?, ?, 1, 'build', 'agent', 'builder', 'success', ?)", (adw_id() + "_01_build", adw_id(), now))
    c.execute("UPDATE sessions SET status = 'success' WHERE adw_id = ?", (adw_id(),))
`),
};

/** An ADW from before --describe: argparse turns the unknown flag down. */
const OLD_ADW = `"""ADW Old — from before --describe.\n\nPhases: engineer(request) -> builder\n"""
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("prompt")
parser.add_argument("--adw-id")
parser.parse_args()
`;

interface Repo { root: string; db: string; port: number }

const SCHEMA = /SCHEMA = """([\s\S]*?)"""/.exec(readFileSync(TRACER, "utf8"))![1]!;

/**
 * The first Run in a fresh repo: once its `hold_db` file is gone it creates the
 * trace db as the tracer does — WAL, then the schema — and writes its row.
 */
const ADW_FIRST = stub(
  "ADW First — creates the trace db, as a first Run's tracer does.\n\nPhases: engineer(request) -> builder\n",
  DESCRIBE_PLAIN,
  `
held("hold_db")
os.makedirs("adws/adw_data", exist_ok=True)
with db() as c:
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(${JSON.stringify(SCHEMA)})
    c.execute("INSERT INTO sessions (adw_id, adw_name, request, status, engineer, started_at)"
              " VALUES (?, 'adw_first', ?, 'running', 'stub', ?)",
              (adw_id(), json.dumps(sys.argv[1:]), datetime.now(timezone.utc).isoformat()))
held()
`);

/**
 * A git repo with the stubs in adws/ and an empty trace db the real tracer's
 * schema built — or, `{ db: false }`, a fresh install no Run has traced yet.
 */
function repo(adws: Record<string, string> = STUBS, { db: withDb = true } = {}): Repo {
  const root = tempDir();
  spawnSync("git", ["init", "-q"], { cwd: root });
  mkdirSync(join(root, "adws"), { recursive: true });
  for (const [name, source] of Object.entries(adws)) writeFileSync(join(root, "adws", name), source, "utf8");
  const path = join(root, "adws", "adw_data", "sssf.db");
  if (withDb) {
    mkdirSync(dirname(path), { recursive: true });
    const db = new Database(path);
    db.exec("PRAGMA journal_mode = WAL");
    db.exec(SCHEMA);
    db.close();
  }
  hold(root);
  onCleanup(() => rmSync(join(root, "hold"), { force: true }));
  return { root, db: path, port: nextPort() };
}

function hold(root: string): void {
  writeFileSync(join(root, "hold"), "", "utf8");
}

function release(root: string): void {
  rmSync(join(root, "hold"), { force: true });
}

function startConsole(r: Repo) {
  return serve([join("server", "index.ts"), "--db", r.db], r.port);
}

async function api<T>(r: Repo, path: string, body?: unknown): Promise<{ status: number; body: T }> {
  const res = await fetch(`http://127.0.0.1:${r.port}${path}`, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const text = (await res.text()).replace(/\r\n/g, "\n");   // a log written on Windows
  let parsed: unknown = text;
  try { parsed = JSON.parse(text); } catch { /* a log is text */ }
  return { status: res.status, body: parsed as T };
}

async function launch(r: Repo, adw: string, values: Record<string, string | boolean>): Promise<Launch> {
  const done = await api<Launch>(r, "/api/launches", { adw, values });
  expect(done.status).toBe(201);
  return done.body;
}

/** Continue a Run the way its card does: preview, then launch exactly what it showed. */
async function continueRun(r: Repo, adwId: string, adw: string, values: Record<string, string | boolean>): Promise<Launch> {
  expect((await api(r, "/api/launches/preview", { adw, values, continues: adwId })).status).toBe(200);
  const done = await api<Launch>(r, "/api/launches", { adw, values, continues: adwId });
  expect(done.status).toBe(201);
  return done.body;
}

/** The trace db's files as they stand on disk: any write changes one. */
function dbFiles(r: Repo): (string | null)[] {
  return ["", "-wal"].map((suffix) => {
    const path = r.db + suffix;
    if (!existsSync(path)) return null;
    const stat = statSync(path);
    return `${stat.size}:${stat.mtimeMs}`;
  });
}

async function launchState(r: Repo, adwId: string): Promise<Launch | undefined> {
  return (await api<Launch[]>(r, "/api/launches")).body.find((l) => l.adw_id === adwId);
}

async function settlesAs(r: Repo, adwId: string, state: Launch["state"]): Promise<Launch> {
  expect(await until(async () => (await launchState(r, adwId))?.state === state, 30)).toBe(true);
  return (await launchState(r, adwId))!;
}

describe("the ADW catalog", () => {
  test("lists every adw_*.py with its summary, phases and --describe output", async () => {
    const r = repo();
    await startConsole(r);
    const { body } = await api<AdwCatalog>(r, "/api/adws");
    expect(body.read_only).toBe(false);
    expect(body.adws.map((a) => a.name)).toEqual(["adw_hang", "adw_ok", "adw_refuse", "adw_resume"]);
    const ok = body.adws.find((a) => a.name === "adw_ok")!;
    expect(ok.file).toBe("adws/adw_ok.py");
    expect(ok.summary).toBe("ADW OK — writes its session row, then waits for the hold.");
    expect(ok.phases).toBe("engineer(request) -> builder -> git(commit)");
    expect(ok.description).toEqual(DESCRIBE_OK);
    expect(body.adws.find((a) => a.name === "adw_resume")!.description!.resumes).toBe(true);
  });

  test("an ADW the engineer adds later shows up, script header and CRLF line endings and all", async () => {
    const r = repo();
    await startConsole(r);
    const header = "#!/usr/bin/env -S uv run\n# /// script\n# dependencies = []\n# ///\n";
    const source = header + stub("ADW Mine — my own chain.\n\nPhases: engineer(request) -> me\n", DESCRIBE_PLAIN, "");
    writeFileSync(join(r.root, "adws", "adw_mine.py"), source.replace(/\n/g, "\r\n"), "utf8");
    const mine = (await api<AdwCatalog>(r, "/api/adws")).body.adws.find((a) => a.name === "adw_mine");
    expect(mine?.summary).toBe("ADW Mine — my own chain.");
    expect(mine?.phases).toBe("engineer(request) -> me");
    expect(mine?.description).toEqual(DESCRIBE_PLAIN);
  });

  test("an ADW whose --describe breaks is shown with its error, and doesn't make the repo read-only", async () => {
    const r = repo({ ...STUBS, "adw_broken.py": `"""ADW Broken - crashes."""\nraise RuntimeError("boom")\n` });
    await startConsole(r);
    const { body } = await api<AdwCatalog>(r, "/api/adws");
    expect(body.read_only).toBe(false);
    const broken = body.adws.find((a) => a.name === "adw_broken")!;
    expect(broken.description).toBeNull();
    expect(broken.error).toContain("boom");
  });

  test("a repo whose ADWs predate --describe is read-only", async () => {
    const r = repo({ "adw_old.py": OLD_ADW });
    await startConsole(r);
    const { body } = await api<AdwCatalog>(r, "/api/adws");
    expect(body.read_only).toBe(true);
    expect(body.adws[0]!.description).toBeNull();
    expect(body.adws[0]!.error).toBeTruthy();
    const refused = await api(r, "/api/launches", { adw: "adw_old", values: { prompt: "x" } });
    expect(refused.status).toBe(409);
  });
});

describe("the confirm step", () => {
  test("previews the exact argv and uv run command, and starts nothing", async () => {
    const r = repo();
    await startConsole(r);
    const { status, body } = await api<LaunchPreview>(r, "/api/launches/preview", {
      adw: "adw_ok", values: { prompt: "add a health endpoint", merge: true, depth: "deep", mood: "sunny" },
    });
    expect(status).toBe(200);
    expect(body.adw_id).toMatch(/^[0-9a-f]{8}$/);
    // Options first and the prompt after `--`, so no prompt can be read as a flag.
    expect(body.argv).toEqual(["uv", "run", "adws/adw_ok.py", "--merge", "--depth", "deep",
      "--mood", "sunny", "--adw-id", body.adw_id, "--", "add a health endpoint"]);
    expect(body.command).toBe(`uv run adws/adw_ok.py --merge --depth deep --mood sunny ` +
      `--adw-id ${body.adw_id} -- 'add a health endpoint'`);
    expect((await api<Launch[]>(r, "/api/launches")).body).toEqual([]);
    expect(existsSync(join(r.root, "adws", "adw_data", "sessions", body.adw_id))).toBe(false);
  });

  test("quotes a prompt full of shell syntax so a shell would read it back unchanged", async () => {
    const r = repo();
    await startConsole(r);
    const { body } = await api<LaunchPreview>(r, "/api/launches/preview", { adw: "adw_ok", values: { prompt: PROMPT } });
    const quoted = body.command.slice(`uv run adws/adw_ok.py --adw-id ${body.adw_id} -- `.length);
    const echoed = spawnSync("sh", ["-c", `printf %s ${quoted}`], { encoding: "utf8" });
    if (echoed.error) return;   // no POSIX shell here to check against
    expect(echoed.stdout).toBe(PROMPT);
  });

  test("turns down options the ADW doesn't take, and more than one landing flag", async () => {
    const r = repo();
    await startConsole(r);
    const cases: [Record<string, string | boolean>, string][] = [
      [{ prompt: "x", nope: true }, "nope"],
      [{ prompt: "x", merge: true, pr: true }, "--merge"],
      [{ prompt: "x", depth: "sideways" }, "sideways"],
      [{ prompt: "" }, "prompt"],
      [{ prompt: "x", adw_id: "beefbeef" }, "adw_id"],
    ];
    for (const [values, named] of cases) {
      const done = await api<{ error: string }>(r, "/api/launches/preview", { adw: "adw_ok", values });
      expect(done.status).toBe(400);
      expect(done.body.error).toContain(named);
    }
    expect((await api(r, "/api/launches/preview", { adw: "adw_resume", values: { prompt: "x" } })).status).toBe(400);
    expect((await api(r, "/api/launches/preview", { adw: "adw_nope", values: { prompt: "x" } })).status).toBe(404);
  });
});

describe("a Launch", () => {
  test("becomes its Run once the session row appears, with the prompt as one argument, unchanged", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_ok", { prompt: PROMPT, merge: true });
    expect(started.state).toBe("starting");
    expect(started.adw).toBe("adw_ok");
    await settlesAs(r, started.adw_id, "started");
    const { body } = await api<SessionDetail>(r, `/api/sessions/${started.adw_id}`);
    expect(JSON.parse(body.session.request!)).toEqual(["--merge", "--adw-id", started.adw_id, "--", PROMPT]);
  });

  test("passes a prompt or value that looks like a flag as text, never as an option", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_ok", { prompt: "--help", mood: "-v" });
    await settlesAs(r, started.adw_id, "started");
    const { body } = await api<SessionDetail>(r, `/api/sessions/${started.adw_id}`);
    expect(JSON.parse(body.session.request!)).toEqual(["--mood=-v", "--adw-id", started.adw_id, "--", "--help"]);
  });

  test("launches only the command its preview showed", async () => {
    const r = repo();
    await startConsole(r);
    const preview = (await api<LaunchPreview>(r, "/api/launches/preview",
      { adw: "adw_ok", values: { prompt: "what I reviewed" } })).body;
    const changed = await api<{ error: string }>(r, "/api/launches",
      { adw: "adw_ok", values: { prompt: "something else", merge: true }, adw_id: preview.adw_id });
    expect(changed.status).toBe(409);
    expect(changed.body.error).toContain("review");
    expect((await api<Launch[]>(r, "/api/launches")).body).toEqual([]);
  });

  test("uses the id its preview minted", async () => {
    const r = repo();
    await startConsole(r);
    const preview = (await api<LaunchPreview>(r, "/api/launches/preview",
      { adw: "adw_ok", values: { prompt: "x" } })).body;
    const done = await api<Launch>(r, "/api/launches", { adw: "adw_ok", values: { prompt: "x" }, adw_id: preview.adw_id });
    expect(done.body.adw_id).toBe(preview.adw_id);
    expect(done.body.command).toBe(preview.command);
    const again = await api(r, "/api/launches", { adw: "adw_ok", values: { prompt: "x" }, adw_id: preview.adw_id });
    expect(again.status).toBe(409);
  });

  test("is Starting while its process is alive and has no session row", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_hang", { prompt: "look around" });
    expect(await until(async () =>
      (await api<string>(r, `/api/launches/${started.adw_id}/log`)).body.includes("resolving dependencies"), 30)).toBe(true);
    expect((await launchState(r, started.adw_id))?.state).toBe("starting");
    release(r.root);
    const refused = await settlesAs(r, started.adw_id, "refused");
    expect(refused.exit_code).toBe(0);
  });

  test("that exits with no session row is Refused, with the ADW's own message", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_refuse", { prompt: "anything" });
    const refused = await settlesAs(r, started.adw_id, "refused");
    expect(refused.exit_code).toBe(3);
    expect(refused.log_tail).toContain("no 'builder' agent in sssf.config.yaml");
    expect(refused.log_tail).not.toContain("preflight line 1\n");
    const log = await api<string>(r, `/api/launches/${started.adw_id}/log`);
    expect(log.status).toBe(200);
    expect(log.body).toContain("preflight line 1\n");
    expect(log.body).toContain("no 'builder' agent");
    expect(existsSync(join(r.root, "adws", "adw_data", "sessions", started.adw_id, "console.log"))).toBe(true);
  });

  test("keeps running when the Console stops, and shows up again through its session row", async () => {
    const r = repo();
    const first = await startConsole(r);
    const started = await launch(r, "adw_ok", { prompt: "outlive me" });
    await settlesAs(r, started.adw_id, "started");
    await stop(first);
    expect(await health(r.port)).toBeNull();

    await startConsole(r);
    expect((await api<Launch[]>(r, "/api/launches")).body).toEqual([]);   // Launches are memory only
    const status = async () => (await api<SessionDetail>(r, `/api/sessions/${started.adw_id}`)).body.session?.status;
    expect(await status()).toBe("running");
    release(r.root);
    // Only a process that outlived the first server can still finish its Run.
    expect(await until(async () => (await status()) === "success", 30)).toBe(true);
    expect((await api<string>(r, `/api/launches/${started.adw_id}/log`)).body).toContain("starting");
  });
});

describe("Continue with…", () => {
  /** A Run adw_ok started and finished, ready to be continued; the hold is back on after. */
  async function settledRun(r: Repo): Promise<string> {
    const started = await launch(r, "adw_ok", { prompt: "plan it" });
    await settlesAs(r, started.adw_id, "started");
    release(r.root);
    const status = async () => (await api<SessionDetail>(r, `/api/sessions/${started.adw_id}`)).body.session?.status;
    expect(await until(async () => (await status()) === "success", 30)).toBe(true);
    hold(r.root);
    return started.adw_id;
  }

  test("previews the Resuming ADW with that Run's adw_id", async () => {
    const r = repo();
    await startConsole(r);
    const runId = await settledRun(r);
    const { status, body } = await api<LaunchPreview>(r, "/api/launches/preview",
      { adw: "adw_resume", values: { prompt: "build the plan" }, continues: runId });
    expect(status).toBe(200);
    expect(body.adw_id).toBe(runId);
    expect(body.argv).toEqual(["uv", "run", "adws/adw_resume.py", "--adw-id", runId, "--", "build the plan"]);
    expect(body.command).toBe(`uv run adws/adw_resume.py --adw-id ${runId} -- 'build the plan'`);
  });

  test("is Starting until the Resuming ADW joins the Run, and its work lands under the same Run", async () => {
    const r = repo();
    await startConsole(r);
    const runId = await settledRun(r);
    const continued = await continueRun(r, runId, "adw_resume", { prompt: "build the plan" });
    expect(continued.adw_id).toBe(runId);
    expect(continued.continuing).toBe(true);
    expect(continued.state).toBe("starting");
    expect(await until(async () =>
      (await api<string>(r, `/api/launches/${runId}/log`)).body.includes("resuming"), 30)).toBe(true);
    expect((await launchState(r, runId))?.state).toBe("starting");

    release(r.root);
    await settlesAs(r, runId, "started");
    const { body } = await api<SessionDetail>(r, `/api/sessions/${runId}`);
    expect(body.session.adw_name).toBe("adw_ok + adw_resume");
    expect(body.phases.map((p) => p.name)).toEqual(["build"]);
    expect((await api<unknown[]>(r, "/api/sessions")).body).toHaveLength(1);
  });

  test("that the Resuming ADW turns down is Refused, with only its own message", async () => {
    const r = repo();
    await startConsole(r);
    const runId = await settledRun(r);
    await continueRun(r, runId, "adw_resume", { prompt: "refuse me" });
    const refused = await settlesAs(r, runId, "refused");
    expect(refused.exit_code).toBe(3);
    expect(refused.log_tail).toContain(`no plan to build under ${runId}`);
    expect(refused.log_tail).not.toContain("starting");   // the first Run's output, earlier in the same log
    expect((await api<string>(r, `/api/launches/${runId}/log`)).body).toContain(`starting ${runId}`);
  });

  test("takes only a Resuming ADW, and only for a Run that exists and has settled", async () => {
    const r = repo();
    await startConsole(r);
    const runId = await settledRun(r);
    const preview = (body: object) => api<{ error: string }>(r, "/api/launches/preview", body);

    const fresh = await preview({ adw: "adw_ok", values: { prompt: "x" }, continues: runId });
    expect(fresh.status).toBe(400);
    expect(fresh.body.error).toContain("Resuming");
    expect((await preview({ adw: "adw_resume", values: { prompt: "x" }, continues: "deadbeef" })).status).toBe(404);
    expect((await preview({ adw: "adw_resume", values: { prompt: "x" }, continues: runId, adw_id: "beefbeef" }))
      .status).toBe(400);

    const live = await launch(r, "adw_ok", { prompt: "still going" });
    await settlesAs(r, live.adw_id, "started");
    const running = await preview({ adw: "adw_resume", values: { prompt: "x" }, continues: live.adw_id });
    expect(running.status).toBe(409);
    expect(running.body.error).toContain("running");
  });

  test("launches only the command its preview showed", async () => {
    const r = repo();
    await startConsole(r);
    const runId = await settledRun(r);
    const unseen = await api<{ error: string }>(r, "/api/launches",
      { adw: "adw_resume", values: { prompt: "x" }, continues: runId });
    expect(unseen.status).toBe(409);
    expect(unseen.body.error).toContain("review");
    await api(r, "/api/launches/preview", { adw: "adw_resume", values: { prompt: "x" }, continues: runId });
    const changed = await api(r, "/api/launches", { adw: "adw_resume", values: { prompt: "y" }, continues: runId });
    expect(changed.status).toBe(409);
    expect((await launchState(r, runId))?.continuing).toBe(false);   // only the first Run's Launch
  });
});

describe("Dismiss", () => {
  test("forgets a Refused Launch for good, and keeps its log", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_refuse", { prompt: "anything" });
    await settlesAs(r, started.adw_id, "refused");
    const before = dbFiles(r);

    const done = await api(r, `/api/launches/${started.adw_id}/dismiss`, {});
    expect(done.status).toBe(200);
    expect(await launchState(r, started.adw_id)).toBeUndefined();
    expect(await launchState(r, started.adw_id)).toBeUndefined();   // the next poll too

    const log = await api<string>(r, `/api/launches/${started.adw_id}/log`);
    expect(log.status).toBe(200);
    expect(log.body).toContain("no 'builder' agent");
    expect(dbFiles(r)).toEqual(before);
  });

  test("turns down a Starting Launch, and an id it doesn't know, and changes nothing", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_hang", { prompt: "look around" });
    expect((await launchState(r, started.adw_id))?.state).toBe("starting");

    const starting = await api<{ error: string }>(r, `/api/launches/${started.adw_id}/dismiss`, {});
    expect(starting.status).toBe(409);
    expect(starting.body.error).toContain("still starting");
    const unknown = await api<{ error: string }>(r, "/api/launches/nosuchlaunch/dismiss", {});
    expect(unknown.status).toBe(404);
    expect((await api<Launch[]>(r, "/api/launches")).body.map((l) => l.adw_id)).toEqual([started.adw_id]);
    expect((await launchState(r, started.adw_id))?.state).toBe("starting");

    release(r.root);
    await settlesAs(r, started.adw_id, "refused");
  });

  test("turns down a Launch that became its Run", async () => {
    const r = repo();
    await startConsole(r);
    const started = await launch(r, "adw_ok", { prompt: "keep me" });
    await settlesAs(r, started.adw_id, "started");
    expect((await api(r, `/api/launches/${started.adw_id}/dismiss`, {})).status).toBe(409);
    expect((await launchState(r, started.adw_id))?.state).toBe("started");
  });
});

describe("before the first Run", () => {
  test("the Console starts with no db, says so, and reads as an empty trace", async () => {
    const r = repo({ ...STUBS, "adw_first.py": ADW_FIRST }, { db: false });
    await startConsole(r);
    const { body: h } = await api<HealthResponse>(r, "/api/health");
    expect(h.db).toBe(r.db);
    expect(h.db_exists).toBe(false);
    expect(h.sessions).toBe(0);
    expect((await api(r, "/api/sessions")).body).toEqual([]);
    // As an empty trace answers: a session lookup is 404, its lists are empty.
    expect((await api(r, "/api/sessions/nosuchrun")).status).toBe(404);
    expect((await api(r, "/api/sessions/nosuchrun/agents/builder/prompts")).status).toBe(404);
    expect((await api(r, "/api/sessions/nosuchrun/events")).body).toEqual({ events: [], cursor: 0, has_more: false });
    expect((await api(r, "/api/sessions/nosuchrun/envelopes")).body).toEqual([]);
    expect((await api(r, "/api/sessions/nosuchrun/gates")).body).toEqual([]);
    expect((await api(r, "/api/sessions/nosuchrun/archive", {})).status).toBe(404);
    expect((await api(r, "/api/sessions/nosuchrun/stop", {})).status).toBe(404);
    expect((await api<AdwCatalog>(r, "/api/adws")).body.adws.map((a) => a.name)).toContain("adw_first");
    expect(existsSync(r.db)).toBe(false);
  });

  test("a Launch is Starting, then its Run once its tracer creates the db — no restart", async () => {
    const r = repo({ ...STUBS, "adw_first.py": ADW_FIRST }, { db: false });
    writeFileSync(join(r.root, "hold_db"), "", "utf8");
    onCleanup(() => rmSync(join(r.root, "hold_db"), { force: true }));
    await startConsole(r);
    const started = await launch(r, "adw_first", { prompt: "the very first" });
    expect((await launchState(r, started.adw_id))?.state).toBe("starting");
    expect((await api(r, "/api/sessions")).body).toEqual([]);
    expect(existsSync(r.db)).toBe(false);

    rmSync(join(r.root, "hold_db"));
    await settlesAs(r, started.adw_id, "started");
    const sessions = (await api<{ adw_id: string }[]>(r, "/api/sessions")).body;
    expect(sessions.map((s) => s.adw_id)).toEqual([started.adw_id]);
    expect((await api<SessionDetail>(r, `/api/sessions/${started.adw_id}`)).status).toBe(200);
    const { body: h } = await api<HealthResponse>(r, "/api/health");
    expect(h.db_exists).toBe(true);
    expect(h.sessions).toBe(1);
    expect(h.journal_mode).toBe("wal");
  });
});

describe("the server", () => {
  test("listens on 127.0.0.1 only", async () => {
    const r = repo();
    await startConsole(r);
    expect(await health(r.port, "127.0.0.1")).not.toBeNull();
    const lan = Object.values(networkInterfaces()).flat()
      .find((i) => i && i.family === "IPv4" && !i.internal)?.address;
    if (lan) expect(await health(r.port, lan)).toBeNull();
  });
});
