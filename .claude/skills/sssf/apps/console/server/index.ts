/**
 * SSSF Console server — JSON API over a target repo's sssf.db, plus the
 * built UI when ./dist exists. Its one db write is
 * POST /api/sessions/:adw_id/archive, which sets one review flag on a row.
 *
 * It also launches ADWs (server/launches.ts). A Launch spawns the ADW, and the
 * ADW's own tracer writes the trace; the Console only reads it back (ADR 0001).
 * Each Launch's log and record sit in its session dir, so a restart shows
 * again the Launches that never became their Run.
 * Stop runs the factory's own verified kill, procs.py (server/stop.ts).
 *
 * It starts before the repo's first Run has created sssf.db, and reads as an
 * empty trace until the tracer does (TraceDb); it never creates the db itself.
 *
 * There is no ingest endpoint and no websocket. The data path is
 * agents → sqlite → web ui, and the UI gets there by polling. It listens on
 * 127.0.0.1 only: there is no auth, and a Launch runs code.
 *
 *   bun run server/index.ts
 *   bun run server/index.ts --db /path/to/repo/adws/adw_data/sssf.db [--repo /path/to/repo]
 *   SSSF_DB=/path/to/sssf.db PORT=4600 bun run server/index.ts
 */
import { existsSync, statSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { TraceDb, resolveDbPath } from "./db.ts";
import { HttpError, Launches, resolveRepoRoot } from "./launches.ts";
import { listReady } from "./issues.ts";
import { stillRunning, stopRun } from "./stop.ts";
import type { AgentPrompts, ApiError, HealthResponse, IssueListing, LaunchRequest } from "../shared/types.ts";

const PORT = Number(process.env.PORT ?? 4600);
const DIST_DIR = resolve(import.meta.dir, "..", "dist");

const dbPath = resolveDbPath();
const trace = new TraceDb(dbPath);
const launches: Launches = new Launches(resolveRepoRoot(dbPath), trace.sessionsDir, {
  session: (adwId) => trace.read((db) => db.session(adwId), null),
  adwProcessCount: (adwId) => trace.read((db) => db.adwProcessCount(adwId), 0),
  claimCount: (adwId) => trace.read((db) => db.claimCount(adwId), 0),
}, async () => (await readyIssues()).tracker,
(pid, argv, adwId) => stillRunning(launches.repoRoot, pid, argv, adwId));
// Today's Launches that never became their Run, read back from their records before any request.
await launches.recover();

/** The Ready issues, each listing also telling the Launches which Tracker this repo uses. */
async function readyIssues(): Promise<IssueListing> {
  const listing = await listReady(launches.repoRoot, {
    claimedRuns: () => trace.read((db) => db.claimedRuns(), new Map()),
    heldIssues: () => launches.heldIssues(),
  });
  launches.learnTracker(listing.tracker);
  return listing;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function notFound(message: string): Response {
  return json({ error: message } satisfies ApiError, 404);
}

/** Guard every handler so a malformed query can't take the server down mid-run. */
function safely(
  handler: (req: Request) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return async (req) => {
    try {
      return await handler(req);
    } catch (error) {
      if (error instanceof HttpError) {
        return json({ error: error.message } satisfies ApiError, error.status);
      }
      console.error(`[sssf] ${req.method} ${new URL(req.url).pathname}:`, error);
      return json({ error: (error as Error).message } satisfies ApiError, 500);
    }
  };
}

/**
 * adw_ids and agent names are path segments on disk, so anything that isn't a
 * plain identifier is rejected outright rather than sanitized into something
 * that might still escape the sessions directory.
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;

function isSafeSegment(value: string): boolean {
  return SAFE_SEGMENT.test(value) && value !== "." && value !== "..";
}

function param(req: Request, key: string): string {
  return decodeURIComponent(
    (req as Request & { params: Record<string, string> }).params[key] ?? "",
  );
}

function intQuery(req: Request, key: string, fallback: number): number {
  const raw = new URL(req.url).searchParams.get(key);
  if (raw === null || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** Serve the built SPA if it has been built; otherwise point at the dev server. */
async function serveStatic(req: Request): Promise<Response> {
  const { pathname } = new URL(req.url);

  if (!existsSync(DIST_DIR)) {
    return new Response(
      `SSSF Console API is running on :${PORT}.\n\n` +
        `No ./dist build found. Run "bun run dev" for the Vite dev server ` +
        `(it proxies /api here), or "bun run build" to serve the UI from this process.\n`,
      { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  // Reject traversal before touching the filesystem.
  const candidate = resolve(join(DIST_DIR, pathname));
  // `sep`, not "/": on Windows resolve() yields C:\...\dist\assets\x.js, and a
  // hardcoded "/" sent every asset to the SPA fallback as text/html.
  if (candidate === DIST_DIR || candidate.startsWith(DIST_DIR + sep)) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return new Response(Bun.file(candidate));
    }
  }

  // SPA fallback: breadcrumb routes are client-side.
  const indexHtml = join(DIST_DIR, "index.html");
  if (existsSync(indexHtml)) {
    return new Response(Bun.file(indexHtml), {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  return notFound("not found");
}

async function launchRequest(req: Request): Promise<LaunchRequest> {
  const body = await req.json().catch(() => null);
  if (typeof body !== "object" || body === null) throw new HttpError(400, "expected a JSON body");
  return body as LaunchRequest;
}

const server = Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",
  routes: {
    "/api/health": safely(() => {
      const db = trace.open();
      return json({
        ok: true,
        service: "sssf-visualizer",
        services: ["sssf-console", "sssf-visualizer"],
        pid: process.pid,
        db: trace.path,
        db_exists: db !== null,
        journal_mode: db?.journalMode ?? null,
        sessions: db?.sessionCount() ?? 0,
      } satisfies HealthResponse);
    }),

    // The repo's ADWs, each with the command line its --describe reports.
    "/api/adws": safely(async () => json(await launches.catalog())),

    // The repo's Ready issues with their verdicts, read fresh each time it is asked:
    // the UI asks on open, on refresh and after each Launch settles, and never on a timer.
    "/api/issues": safely(async () => json(await readyIssues())),

    "/api/launches": {
      GET: safely(() => json(launches.list())),
      POST: safely(async (req) => json(await launches.start(await launchRequest(req)), 201)),
    },

    // The confirm step: the argv and command a Launch would run. Starts nothing.
    "/api/launches/preview": {
      POST: safely(async (req) => json(await launches.preview(await launchRequest(req)))),
    },

    // Forget a Refused Launch. Its log stays, and the trace is never written.
    "/api/launches/:adw_id/dismiss": {
      POST: safely((req) => {
        const adwId = param(req, "adw_id");
        launches.dismiss(adwId);
        return json({ adw_id: adwId, dismissed: true });
      }),
    },

    // Everything the ADW printed: for a Refused Launch, the only record there is.
    "/api/launches/:adw_id/log": safely((req) => {
      const path = launches.logPath(param(req, "adw_id"));
      return path
        ? new Response(Bun.file(path), {
            headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
          })
        : notFound(`no launch log for ${param(req, "adw_id")}`);
    }),

    "/api/sessions": safely((req) => json(trace.read((db) => db.sessions(intQuery(req, "limit", 200)), []))),

    "/api/sessions/:adw_id": safely((req) => {
      const detail = trace.read((db) => db.sessionDetail(param(req, "adw_id")), null);
      return detail ? json(detail) : notFound(`no session ${param(req, "adw_id")}`);
    }),

    // The one write. Archiving is review triage — it belongs to the reader, not
    // to the run — so it never touches anything a tracer wrote.
    "/api/sessions/:adw_id/archive": {
      POST: safely(async (req) => {
        const adwId = param(req, "adw_id");
        if (!isSafeSegment(adwId)) {
          return json({ error: "invalid adw_id" } satisfies ApiError, 400);
        }
        const body = (await req.json().catch(() => ({}))) as { archived?: unknown };
        const archived = body.archived === undefined ? true : Boolean(body.archived);
        return trace.read((db) => db.setArchived(adwId, archived), false)
          ? json({ adw_id: adwId, archived })
          : notFound(`no session ${adwId}`);
      }),
    },

    // Stop a running Run with the factory's verified kill. The Run settles its
    // own trace; only one that can't is closed by procs.py, never by this server.
    "/api/sessions/:adw_id/stop": {
      // With no db there is no Run to stop, and procs.py's connection would create the file.
      POST: safely(async (req) => trace.open()
        ? json(await stopRun(launches.repoRoot, trace.path, param(req, "adw_id")))
        : notFound(`no session ${param(req, "adw_id")}`)),
    },

    "/api/sessions/:adw_id/events": safely((req) =>
      json(
        trace.read((db) => db.events(
          param(req, "adw_id"),
          intQuery(req, "after", 0),
          intQuery(req, "limit", 500),
        ), { events: [], cursor: Math.max(0, intQuery(req, "after", 0)), has_more: false }),
      ),
    ),

    "/api/sessions/:adw_id/envelopes": safely((req) =>
      json(trace.read((db) => db.envelopes(param(req, "adw_id")), [])),
    ),

    "/api/sessions/:adw_id/gates": safely((req) => json(trace.read((db) => db.gates(param(req, "adw_id")), []))),

    // The exact prompts an agent was sent, read from the session dir. Files are
    // the raw record; the db has no copy of them.
    "/api/sessions/:adw_id/agents/:agent/prompts": safely(async (req) => {
      const adwId = param(req, "adw_id");
      const agent = param(req, "agent");
      if (!isSafeSegment(adwId) || !isSafeSegment(agent)) {
        return json({ error: "invalid adw_id or agent" } satisfies ApiError, 400);
      }
      if (!trace.read((db) => db.session(adwId), null)) return notFound(`no session ${adwId}`);

      const dir = resolve(trace.sessionsDir, adwId, agent, "prompts");
      // Defense in depth: the segment check already forbids traversal.
      if (dir !== trace.sessionsDir && !dir.startsWith(trace.sessionsDir + sep)) {
        return json({ error: "invalid path" } satisfies ApiError, 400);
      }

      // A prompt file is absent whenever the agent never ran in this session —
      // a normal state, so it reads as null rather than an error.
      const read = async (name: string): Promise<string | null> => {
        const file = Bun.file(join(dir, `${name}.md`));
        return (await file.exists()) ? await file.text() : null;
      };
      return json({
        system: await read("system"),
        user: await read("user"),
      } satisfies AgentPrompts);
    }),
  },

  fetch(req) {
    const { pathname } = new URL(req.url);
    if (pathname.startsWith("/api/")) return notFound(`no route ${pathname}`);
    return serveStatic(req);
  },
});

console.log(`[sssf] Console api     http://127.0.0.1:${server.port}`);
const db = trace.open();
console.log(db
  ? `[sssf] db              ${trace.path}  [journal_mode=${db.journalMode}]`
  : `[sssf] db              ${trace.path}  (not there yet: the first Run's tracer creates it)`);
console.log(`[sssf] repo            ${launches.repoRoot}`);
console.log(
  existsSync(DIST_DIR)
    ? `[sssf] serving ui from  ${DIST_DIR}`
    : `[sssf] no ./dist — use "bun run dev" for the Vite dev server on :4601`,
);

process.on("SIGINT", () => {
  trace.close();
  process.exit(0);
});
