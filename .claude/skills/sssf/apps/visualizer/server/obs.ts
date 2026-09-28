/**
 * Run the visualizer in the background without taking over the terminal.
 *
 *   bun run server/obs.ts start --db /path/to/repo/adws/adw_data/sssf.db
 *   bun run server/obs.ts stop
 *   bun run server/obs.ts status
 *
 * There is no pid file to go stale. The running server reports its own pid
 * and identity on /api/health, so `stop` asks the server which process it is
 * and ends exactly that one — never a stranger that happens to hold the port.
 * `start` on a running server just prints its URL.
 */
import { spawn, spawnSync } from "node:child_process";
import { openSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { HealthResponse } from "../shared/types.ts";

const PORT = Number(process.env.PORT ?? 4600);
const URL_BASE = `http://localhost:${PORT}`;
const APP_DIR = resolve(import.meta.dir, "..");
const LOG = join(tmpdir(), `sssf-obs-${PORT}.log`);

type Probe =
  | { state: "ours"; health: HealthResponse }
  | { state: "stranger" }
  | { state: "free" };

async function probe(): Promise<Probe> {
  try {
    const res = await fetch(`${URL_BASE}/api/health`, { signal: AbortSignal.timeout(1500) });
    const body = (await res.json().catch(() => ({}))) as Partial<HealthResponse>;
    return body.service === "sssf-visualizer"
      ? { state: "ours", health: body as HealthResponse }
      : { state: "stranger" };
  } catch {
    return { state: "free" };   // nothing listening (or not answering HTTP)
  }
}

async function waitFor(done: () => Promise<boolean>, seconds: number): Promise<boolean> {
  for (const end = Date.now() + seconds * 1000; Date.now() < end; ) {
    if (await done()) return true;
    await Bun.sleep(250);
  }
  return false;
}

function running(health: HealthResponse): void {
  console.log(`[sssf] trace UI  ${URL_BASE}   (pid ${health.pid}, stop: just obs-stop)`);
  console.log(`[sssf] db        ${health.db}`);
}

async function start(): Promise<number> {
  const found = await probe();
  if (found.state === "ours") {
    running(found.health);
    return 0;
  }
  if (found.state === "stranger") {
    console.error(`[sssf] port ${PORT} is held by something that is not the visualizer — ` +
      `free it or set PORT`);
    return 1;
  }

  // Build first, in the foreground, so a broken build is seen here and not in a log.
  const build = spawnSync(process.execPath, ["x", "vite", "build", "--logLevel", "warn"],
    { cwd: APP_DIR, stdio: "inherit" });
  if (build.status !== 0) return build.status ?? 1;

  // Detached, output to a log, handle released: the server outlives this
  // command and the terminal, and Ctrl+C in the terminal cannot reach it.
  const log = openSync(LOG, "w");
  const child = spawn(process.execPath, [join(APP_DIR, "server", "index.ts"), ...process.argv.slice(3)],
    { cwd: APP_DIR, detached: true, windowsHide: true, stdio: ["ignore", log, log],
      env: { ...process.env, PORT: String(PORT) } });
  let exited = false;
  child.on("exit", () => { exited = true; });
  child.unref();

  const up = await waitFor(async () => exited || (await probe()).state === "ours", 20);
  const now = await probe();
  if (!up || now.state !== "ours") {
    console.error(`[sssf] visualizer did not start — log: ${LOG}\n`);
    console.error(readFileSync(LOG, "utf8").trim().split("\n").slice(-15).join("\n"));
    return 1;
  }
  running(now.health);
  console.log(`[sssf] log       ${LOG}`);
  return 0;
}

async function stop(): Promise<number> {
  const found = await probe();
  if (found.state !== "ours") {
    console.log(found.state === "free"
      ? `[sssf] visualizer is not running on :${PORT}`
      : `[sssf] port ${PORT} is held by something that is not the visualizer — left alone`);
    return 0;
  }
  process.kill(found.health.pid);
  if (!(await waitFor(async () => (await probe()).state === "free", 10))) {
    console.error(`[sssf] pid ${found.health.pid} did not stop within 10s`);
    return 1;
  }
  console.log(`[sssf] visualizer stopped (pid ${found.health.pid})`);
  return 0;
}

async function status(): Promise<number> {
  const found = await probe();
  if (found.state === "ours") running(found.health);
  else if (found.state === "free") console.log(`[sssf] visualizer is not running on :${PORT}`);
  else console.log(`[sssf] port ${PORT} is held by something that is not the visualizer`);
  return 0;
}

const commands: Record<string, () => Promise<number>> = { start, stop, status };
const command = commands[process.argv[2] ?? ""];
if (!command) {
  console.error("usage: bun run server/obs.ts start [--db <path>] | stop | status");
  process.exit(2);
}
process.exit(await command());
