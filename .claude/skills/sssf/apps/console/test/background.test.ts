/**
 * The background tooling (server/background.ts) and the health response it
 * reads, across the rename from visualizer to Console: a server started under
 * either name is recognised, stopped, and never mistaken for a stranger.
 *
 *   bun test
 */
import { Database } from "bun:sqlite";
import { afterEach, describe, expect, setDefaultTimeout, test } from "bun:test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const APP_DIR = resolve(import.meta.dir, "..");
const BACKGROUND = join(APP_DIR, "server", "background.ts");

setDefaultTimeout(30_000);

let nextPort = 4700 + Math.floor(Math.random() * 1000);
const children: ChildProcess[] = [];
const dirs: string[] = [];

afterEach(async () => {
  // Wait for each exit: on Windows a server still holds its db until it is gone.
  await Promise.all(children.splice(0).map((child) =>
    child.exitCode !== null || child.signalCode !== null
      ? undefined
      : new Promise((done) => { child.once("exit", done); child.kill(); })));
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true, maxRetries: 10 });
});

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "sssf-console-test-"));
  dirs.push(dir);
  return dir;
}

/** A trace db with nothing in it, shaped like one a tracer created. */
function emptyDb(): string {
  const path = join(tempDir(), "sssf.db");
  const db = new Database(path);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec("CREATE TABLE sessions (adw_id TEXT PRIMARY KEY)");
  db.close();
  return path;
}

async function health(port: number): Promise<Record<string, unknown> | null> {
  try {
    const res = await fetch(`http://localhost:${port}/api/health`, { signal: AbortSignal.timeout(1000) });
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

async function until(done: () => Promise<boolean>, seconds = 15): Promise<boolean> {
  for (const end = Date.now() + seconds * 1000; Date.now() < end; ) {
    if (await done()) return true;
    await Bun.sleep(100);
  }
  return false;
}

/** Start something on a port and wait until it answers /api/health. */
async function serve(argv: string[], port: number): Promise<ChildProcess> {
  const child = spawn(process.execPath, argv, {
    cwd: APP_DIR, stdio: "ignore", env: { ...process.env, PORT: String(port) },
  });
  children.push(child);
  expect(await until(async () => (await health(port)) !== null)).toBe(true);
  return child;
}

/** A server from before the rename: its health names only the old service. */
function preRenameServer(service: string): string {
  const script = join(tempDir(), "old.ts");
  Bun.write(script, `Bun.serve({ port: Number(process.env.PORT), routes: {
    "/api/health": () => Response.json({ ok: true, service: ${JSON.stringify(service)},
      pid: process.pid, db: "old.db", journal_mode: "wal", sessions: 0 }) } });`);
  return script;
}

function tooling(command: string, port: number): { status: number | null; out: string } {
  const done = spawnSync(process.execPath, [BACKGROUND, command], {
    cwd: APP_DIR, encoding: "utf8", env: { ...process.env, PORT: String(port) },
  });
  return { status: done.status, out: `${done.stdout}${done.stderr}` };
}

describe("health", () => {
  test("names the Console and, for one release, the visualizer it was", async () => {
    const port = nextPort++;
    await serve([join("server", "index.ts"), "--db", emptyDb()], port);
    const body = await health(port);
    expect(body?.services).toEqual(["sssf-console", "sssf-visualizer"]);
    // Tooling from before the rename recognises a server by this field alone.
    expect(body?.service).toBe("sssf-visualizer");
  });
});

describe("background tooling", () => {
  test("stops a Console it started under the new name", async () => {
    const port = nextPort++;
    await serve([join("server", "index.ts"), "--db", emptyDb()], port);
    expect(tooling("status", port).out).toContain("Console");
    const stopped = tooling("stop", port);
    expect(stopped.status).toBe(0);
    expect(stopped.out).toContain("Console stopped");
    expect(await until(async () => (await health(port)) === null)).toBe(true);
  });

  test("stops a server started before the rename", async () => {
    const port = nextPort++;
    await serve([preRenameServer("sssf-visualizer")], port);
    expect(tooling("status", port).out).toContain("Console");
    const stopped = tooling("stop", port);
    expect(stopped.status).toBe(0);
    expect(stopped.out).toContain("Console stopped");
    expect(await until(async () => (await health(port)) === null)).toBe(true);
  });

  test("stops a server that already reports only the new name", async () => {
    const port = nextPort++;
    await serve([preRenameServer("sssf-console")], port);
    expect(tooling("stop", port).status).toBe(0);
    expect(await until(async () => (await health(port)) === null)).toBe(true);
  });

  test("leaves a stranger on the port alone", async () => {
    const port = nextPort++;
    await serve([preRenameServer("something-else")], port);
    const stopped = tooling("stop", port);
    expect(stopped.out).toContain("not the Console");
    expect(await health(port)).not.toBeNull();
  });
});
