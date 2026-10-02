/**
 * What the server tests share: temp dirs, waiting, and starting a server on a
 * port of its own. Every process and dir is cleaned up after each test.
 */
import { afterEach } from "bun:test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

export const APP_DIR = resolve(import.meta.dir, "..");

let nextPort = 4700 + Math.floor(Math.random() * 1000);
const children: ChildProcess[] = [];
const dirs: string[] = [];
const cleanups: (() => void)[] = [];

afterEach(async () => {
  for (const cleanup of cleanups.splice(0)) cleanup();
  // Wait for each exit: on Windows a server still holds its db until it is gone.
  await Promise.all(children.splice(0).map(stop));
  await Promise.all(dirs.splice(0).map(remove));
});

/** On Windows a dir stays locked while a process that ran in it is still exiting. */
async function remove(dir: string): Promise<void> {
  for (let attempt = 1; ; attempt++) {
    try {
      rmSync(dir, { recursive: true, force: true });
      return;
    } catch (error) {
      if (attempt >= 60) throw error;
      await Bun.sleep(250);
    }
  }
}

export function port(): number {
  return nextPort++;
}

export function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "sssf-console-test-"));
  dirs.push(dir);
  return dir;
}

/** Run before the processes are stopped and the dirs removed. */
export function onCleanup(cleanup: () => void): void {
  cleanups.push(cleanup);
}

export async function until(done: () => Promise<boolean>, seconds = 15): Promise<boolean> {
  for (const end = Date.now() + seconds * 1000; Date.now() < end; ) {
    if (await done()) return true;
    await Bun.sleep(100);
  }
  return false;
}

export async function health(port: number, host = "127.0.0.1"): Promise<Record<string, unknown> | null> {
  try {
    const res = await fetch(`http://${host}:${port}/api/health`, { signal: AbortSignal.timeout(1000) });
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** Start something on a port and wait until it answers /api/health. */
export async function serve(argv: string[], port: number): Promise<ChildProcess> {
  const child = spawn(process.execPath, argv, {
    cwd: APP_DIR, stdio: "ignore", env: { ...process.env, PORT: String(port) },
  });
  children.push(child);
  if (!(await until(async () => (await health(port)) !== null))) {
    throw new Error(`nothing answered on :${port} for ${argv.join(" ")}`);
  }
  return child;
}

export function stop(child: ChildProcess): Promise<unknown> | undefined {
  if (child.exitCode !== null || child.signalCode !== null) return undefined;
  return new Promise((done) => { child.once("exit", done); child.kill(); });
}
