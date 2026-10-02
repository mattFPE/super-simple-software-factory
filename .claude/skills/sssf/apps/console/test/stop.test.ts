/**
 * Stopping a Run from the Console, through the server's HTTP API alone (#10).
 *
 * A throwaway git repo holding the factory's real adw_modules and two stub
 * ADWs, each launched for real with `uv run`. adw_settles is built on the real
 * session module and waits on a fake agent: Stop kills the agent and the Run
 * settles itself. adw_stuck writes its trace rows by hand and then hangs,
 * never listening for a stop: Stop must still kill both, agent first, and
 * close the Run as stopped for it.
 *
 *   bun test
 */
import { Database } from "bun:sqlite";
import { describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawn, spawnSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type { Launch, SessionDetail, StopReport } from "../shared/types.ts";
import { APP_DIR, port as nextPort, serve, stop as stopChild, tempDir, until } from "./support.ts";

setDefaultTimeout(90_000);

const MODULES = resolve(APP_DIR, "..", "..", "templates", "adws", "adw_modules");

const STUCK = `"""ADW Stuck — starts an agent, then hangs.\n\nPhases: engineer(request) -> builder\n"""
import json, os, pathlib, sqlite3, subprocess, sys, time
from datetime import datetime, timezone

if "--describe" in sys.argv:
    print(json.dumps({"commits": False, "resumes": False, "mutually_exclusive": [], "options": [
        {"name": "prompt", "flag": None, "kind": "value", "help": None, "default": None,
         "choices": None, "required": True},
        {"name": "adw_id", "flag": "--adw-id", "kind": "value", "help": None, "default": None,
         "choices": None, "required": False}]}))
    sys.exit(0)

adw_id = sys.argv[sys.argv.index("--adw-id") + 1]
now = datetime.now(timezone.utc).isoformat()
agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "fake-agent"],
                         start_new_session=sys.platform != "win32")
with sqlite3.connect("adws/adw_data/sssf.db", timeout=10) as c:
    c.execute("INSERT INTO sessions (adw_id, adw_name, status, started_at) VALUES (?, 'adw_stuck', 'running', ?)",
              (adw_id, now))
    c.execute("INSERT INTO phases (phase_id, adw_id, seq, name, kind, owner, status, started_at)"
              " VALUES (?, ?, 1, 'build', 'agent', 'builder', 'running', ?)", (adw_id + "_01_build", adw_id, now))
    c.execute("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) VALUES (?, 'adw', '', ?, ?, ?)",
              (adw_id, os.getpid(), " ".join([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]), now))
    c.execute("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) VALUES (?, 'agent', 'builder', ?, ?, ?)",
              (adw_id, agent.pid, " ".join(agent.args), now))
pathlib.Path(f"{adw_id}.pids").write_text(f"{os.getpid()} {agent.pid}", encoding="utf-8")
while True:
    time.sleep(1)
`;

const SETTLES = `# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Settles — waits on an agent that never answers, until it is stopped.

Phases: builder
"""
import argparse, pathlib, subprocess, sys
from adw_modules import procs, session
from adw_modules.data_types import PhaseParams, SSSFConfig

parser = argparse.ArgumentParser()
parser.add_argument("prompt")
session.add_cli_args(parser)
args = parser.parse_args()
run = session.ensure(SSSFConfig(), args.adw_id)
run.when_settled(lambda ok: pathlib.Path(f"{run.adw_id}.settled").write_text(
    f"ok={ok} stopped={run.stopped}", encoding="utf-8"))
with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                           description="Wait on an agent that never answers, to be stopped")):
    agent = subprocess.Popen([sys.executable, "-c",
                              "import time; print('ready', flush=True); time.sleep(300)"],
                             stdout=subprocess.PIPE, text=True, **procs.popen_kwargs())
    run.tracer.process_start(run.adw_id, "agent", "builder", agent.pid,
                             procs.recorded_command(agent.args))
    with procs.supervise(agent, 0, "fake agent"):
        for line in agent.stdout:
            pathlib.Path(f"{run.adw_id}.pids").write_text(f"{__import__('os').getpid()} {agent.pid}",
                                                          encoding="utf-8")
    raise SystemExit("the agent ended without anyone stopping it")
`;

interface Repo { root: string; db: string; port: number }

function repo(): Repo {
  const root = tempDir();
  spawnSync("git", ["init", "-q"], { cwd: root });
  mkdirSync(join(root, "adws", "adw_modules"), { recursive: true });
  mkdirSync(join(root, "adws", "adw_data"), { recursive: true });
  writeFileSync(join(root, "adws", "adw_stuck.py"), STUCK, "utf8");
  writeFileSync(join(root, "adws", "adw_settles.py"), SETTLES, "utf8");
  cpSync(MODULES, join(root, "adws", "adw_modules"),
    { recursive: true, filter: (path) => !path.includes("__pycache__") });
  const schema = /SCHEMA = """([\s\S]*?)"""/.exec(readFileSync(join(MODULES, "tracer.py"), "utf8"))![1]!;
  const path = join(root, "adws", "adw_data", "sssf.db");
  const db = new Database(path);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec(schema);
  db.close();
  return { root, db: path, port: nextPort() };
}

async function api<T>(r: Repo, path: string, body?: unknown): Promise<{ status: number; body: T }> {
  const res = await fetch(`http://127.0.0.1:${r.port}${path}`, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  return { status: res.status, body: (await res.json()) as T };
}

/** Whether a process exists. Signal 0 probes on POSIX; Windows has no probe but tasklist. */
function alive(pid: number): boolean {
  if (process.platform !== "win32") {
    try { process.kill(pid, 0); return true; } catch { return false; }
  }
  const out = spawnSync("tasklist", ["/FI", `PID eq ${pid}`, "/NH"], { encoding: "utf8" }).stdout;
  return out.includes(` ${pid} `);
}

async function launched(r: Repo, adw: string): Promise<{ adwId: string; adwPid: number; agentPid: number }> {
  const done = await api<Launch>(r, "/api/launches", { adw, values: { prompt: "hang" } });
  expect(done.status).toBe(201);
  const adwId = done.body.adw_id;
  const pids = join(r.root, `${adwId}.pids`);
  expect(await until(async () => existsSync(pids), 60)).toBe(true);
  const [adwPid, agentPid] = readFileSync(pids, "utf8").split(" ").map(Number) as [number, number];
  return { adwId, adwPid, agentPid };
}

const status = async (r: Repo, adwId: string) =>
  (await api<SessionDetail>(r, `/api/sessions/${adwId}`)).body.session?.status;

describe("Stop", () => {
  test("kills a Run's agent, and the Run settles itself as stopped", async () => {
    const r = repo();
    await serve([join("server", "index.ts"), "--db", r.db], r.port);
    const { adwId, adwPid, agentPid } = await launched(r, "adw_settles");

    const stopped = await api<StopReport>(r, `/api/sessions/${adwId}/stop`, {});

    expect(stopped.status).toBe(200);
    expect(stopped.body.status).toBe("stopped");
    expect(stopped.body.settled_by).toBe("run");
    expect(stopped.body.processes.map((p) => [p.kind, p.pid, p.outcome]))
      .toEqual([["agent", agentPid, "killed"], ["adw", adwPid, "stopped"]]);
    expect(await until(async () => !alive(adwPid) && !alive(agentPid), 15)).toBe(true);
    expect(await status(r, adwId)).toBe("stopped");
    expect(readFileSync(join(r.root, `${adwId}.settled`), "utf8")).toBe("ok=False stopped=True");
  });

  test("kills a hanging Run that never listens, agent first, and closes it as stopped for it", async () => {
    const r = repo();
    await serve([join("server", "index.ts"), "--db", r.db], r.port);
    const { adwId, adwPid, agentPid } = await launched(r, "adw_stuck");
    expect(alive(adwPid)).toBe(true);

    const stopped = await api<StopReport>(r, `/api/sessions/${adwId}/stop`, {});

    expect(stopped.status).toBe(200);
    expect(stopped.body.status).toBe("stopped");
    expect(stopped.body.processes.map((p) => [p.kind, p.pid])).toEqual([["agent", agentPid], ["adw", adwPid]]);
    expect(stopped.body.settled_by).toBe("stop");
    expect(stopped.body.processes.every((p) => p.outcome === "killed" || p.outcome === "stopped")).toBe(true);
    expect(await until(async () => !alive(adwPid) && !alive(agentPid), 15)).toBe(true);
    expect(await status(r, adwId)).toBe("stopped");
  });

  test("never signals a pid that now runs something other than what was recorded, and says so", async () => {
    const r = repo();
    await serve([join("server", "index.ts"), "--db", r.db], r.port);
    const stranger = spawn(process.execPath, ["-e", "setTimeout(() => {}, 300_000)"], { stdio: "ignore" });
    const db = new Database(r.db);
    db.run("INSERT INTO sessions (adw_id, status, started_at) VALUES ('recycled', 'running', '2026-01-01T00:00:00Z')");
    db.run("INSERT INTO processes (adw_id, kind, name, pid, command, started_at) VALUES " +
      "('recycled', 'adw', '', ?, 'adw_build.py --adw-id recycled -- fix it', '2026-01-01T00:00:00Z')", [stranger.pid!]);
    db.close();

    try {
      const stopped = await api<StopReport>(r, "/api/sessions/recycled/stop", {});
      expect(stopped.status).toBe(200);
      const [process] = stopped.body.processes;
      expect(process!.outcome).toBe("mismatch");
      expect(process!.pid).toBe(stranger.pid!);
      expect(alive(stranger.pid!)).toBe(true);
    } finally {
      await stopChild(stranger);
    }
  });

  test("turns down a Run that isn't running", async () => {
    const r = repo();
    await serve([join("server", "index.ts"), "--db", r.db], r.port);
    const db = new Database(r.db);
    db.run("INSERT INTO sessions (adw_id, status, started_at) VALUES ('settled', 'success', '2026-01-01T00:00:00Z')");
    db.close();

    const settled = await api<{ error: string }>(r, "/api/sessions/settled/stop", {});
    const unknown = await api<{ error: string }>(r, "/api/sessions/nosuchrun/stop", {});

    expect(settled.status).toBe(409);
    expect(settled.body.error).toContain("not running");
    expect(unknown.status).toBe(409);
    expect(unknown.body.error).toContain("no Run");
  });
});
