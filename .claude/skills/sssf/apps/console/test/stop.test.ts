/**
 * Stopping a Run from the Console, through the server's HTTP API alone (#10).
 *
 * A throwaway git repo holding the factory's real `procs.py` and one stub ADW,
 * adw_stuck, launched for real with `uv run`: it writes its session row,
 * starts a fake agent, records both processes the way the tracer does, and
 * then hangs, never settling and never listening for a stop. Stop must still
 * kill both, agent first, and close the Run as stopped.
 *
 *   bun test
 */
import { Database } from "bun:sqlite";
import { describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawn, spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
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

interface Repo { root: string; db: string; port: number }

function repo(): Repo {
  const root = tempDir();
  spawnSync("git", ["init", "-q"], { cwd: root });
  mkdirSync(join(root, "adws", "adw_modules"), { recursive: true });
  mkdirSync(join(root, "adws", "adw_data"), { recursive: true });
  writeFileSync(join(root, "adws", "adw_stuck.py"), STUCK, "utf8");
  copyFileSync(join(MODULES, "procs.py"), join(root, "adws", "adw_modules", "procs.py"));
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

async function stuckRun(r: Repo): Promise<{ adwId: string; adwPid: number; agentPid: number }> {
  const done = await api<Launch>(r, "/api/launches", { adw: "adw_stuck", values: { prompt: "hang" } });
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
  test("kills a hanging Run's agent and then the ADW, and the Run settles as stopped", async () => {
    const r = repo();
    await serve([join("server", "index.ts"), "--db", r.db], r.port);
    const { adwId, adwPid, agentPid } = await stuckRun(r);
    expect(alive(adwPid)).toBe(true);

    const stopped = await api<StopReport>(r, `/api/sessions/${adwId}/stop`, {});

    expect(stopped.status).toBe(200);
    expect(stopped.body.status).toBe("stopped");
    expect(stopped.body.processes.map((p) => [p.kind, p.pid])).toEqual([["agent", agentPid], ["adw", adwPid]]);
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
