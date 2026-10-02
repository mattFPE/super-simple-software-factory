# /// script
# dependencies = []
# ///
"""Child-process control for coding agents: idle watchdog, tree kill, and stop.

A coding agent that hangs does not fail — it goes quiet. No events, no tokens,
an ADW blocked forever on a read. And killing it is not one pid: pi's Windows
launcher runs the real pi as a child, and agents spawn shells and subagents of
their own, so killing the pid you started orphans the process doing the work.
Both adapters run their child inside `supervise()`, which kills the whole tree
when the agent goes silent too long, or when the ADW itself is leaving early.

Stopping a whole Run from outside it is `stop()`, and this file is also its
command line — `just kill` and the Console's Stop both run it:

    uv run adws/adw_modules/procs.py stop <adw_id> [--db adws/adw_data/sssf.db] [--json]

It uses only the standard library, so it runs as a plain script.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def popen_kwargs() -> dict:
    """Extra Popen kwargs that make the child's tree killable as one unit.

    POSIX: a new session makes the child a process-group leader, so killpg
    reaches every descendant. (It also stops a terminal Ctrl+C reaching the
    child directly — `supervise()` kills the tree on the way out instead.)
    Windows needs nothing: `taskkill /T` walks the tree by parent pid.
    """
    return {} if sys.platform == "win32" else {"start_new_session": True}


def kill_tree(pid: int) -> None:
    """Kill a process and every descendant. Never raises.

    On POSIX the group is killed only when `pid` leads it (`popen_kwargs()`).
    Otherwise its group is someone else's — an ADW started from a terminal
    shares the shell's — and only the process itself is killed.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        elif os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists right now. Never signals it.

    Not `os.kill(pid, 0)`: on POSIX that is the standard probe, but on Windows
    Python maps any signal other than CTRL_C/CTRL_BREAK to TerminateProcess —
    the "probe" would kill the process it asked about.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                      # exists, owned by someone else
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5              # ERROR_ACCESS_DENIED: it exists
    try:
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259                         # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class StalledStart(RuntimeError):
    """The agent never got going: no sign of a started session within the
    startup window. Nothing was sent yet, so the send is safe to repeat."""


class Watchdog:
    """Kills a process tree after `idle_seconds` with no `touch()`. 0 disables.

    With `start_seconds`, it also kills the tree when `started()` has not been
    called that long after launch — a child that prints a line or two and then
    stalls before its session exists would otherwise sit out the whole idle
    window (#6). `stalled` then says which of the two fired.
    """

    def __init__(self, pid: int, idle_seconds: int, start_seconds: int = 0):
        self.pid = pid
        self.idle_seconds = idle_seconds
        self.start_seconds = start_seconds if idle_seconds > 0 else 0
        self.fired = False
        self.stalled = False
        self._launched = self._last = time.monotonic()
        self._started = not self.start_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def touch(self) -> None:
        self._last = time.monotonic()

    def started(self) -> None:
        self._started = True

    def _watch(self) -> None:
        shortest = min(t for t in (self.idle_seconds, self.start_seconds) if t > 0)
        while not self._stop.wait(min(5.0, max(0.5, shortest / 10))):
            now = time.monotonic()
            stalled = not self._started and now - self._launched > self.start_seconds
            if stalled or now - self._last > self.idle_seconds:
                self.fired, self.stalled = True, stalled
                kill_tree(self.pid)
                return

    def start(self) -> "Watchdog":
        if self.idle_seconds > 0:
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


@contextmanager
def supervise(process: subprocess.Popen, idle_seconds: int, label: str,
              start_seconds: int = 0):
    """Run the body (the output read loop) under an idle watchdog.

    Call `touch()` on the yielded watchdog for every line of output. On a clean
    exit the child has already ended. If the body is left early — Ctrl+C, a
    kill signal turned SystemExit, any exception — the child tree is killed so
    no agent outlives its ADW. If the watchdog fired, raise a readable error
    instead of letting the empty result be mistaken for a bad response.
    """
    dog = Watchdog(process.pid, idle_seconds, start_seconds).start()
    try:
        yield dog
    except BaseException:
        kill_tree(process.pid)
        raise
    finally:
        dog.stop()
    if dog.stalled:
        process.wait()
        raise StalledStart(f"{label} did not start a session within {dog.start_seconds}s "
                           f"and was killed (process tree of pid {process.pid}).")
    if dog.fired:
        process.wait()
        raise RuntimeError(f"{label} produced no output for {idle_seconds}s and was killed "
                           f"(process tree of pid {process.pid}). Raise idle_timeout_seconds "
                           "in sssf.config.yaml if the agent legitimately works in silence.")


# ── stopping a Run from outside it ──────────────────────────────────────────
#
# The trace records every process a Run starts (`processes`: the ADW itself and
# each coding agent) with the command it ran. Stopping reads those rows, so a
# hung Run is stoppable by adw_id alone, and checks each pid against its
# recorded command before signalling it: pids get recycled, and a stop must
# never hit whatever process got the number next.
#
# The Run settles itself, exactly as on Ctrl+C. On POSIX a SIGTERM reaches it.
# On Windows no signal reaches a process without killing it outright, so the
# ADW watches its session dir for a stop request (session.ensure starts the
# watcher) and acknowledges it by removing the file. Its agents are killed
# only after that acknowledgement — an ADW whose agent simply died would retry
# or fail the phase, instead of settling as stopped.

STOP_REQUEST = "stop"     # in sessions/<adw_id>/: the Run is being stopped
ACK_SECONDS = 5           # how long a Run has to acknowledge the request
GRACE_SECONDS = 30        # how long it has to settle itself (an issue comment, say)


def recorded_command(argv) -> str:
    """What the trace records as a process's command: its argv, space-joined."""
    return " ".join(str(arg) for arg in argv)[:500]


def _comparable(command: str) -> str:
    """A command line with the quoting and separators that differ by platform removed."""
    text = command.replace("\\", "/").replace('"', "").replace("'", "")
    return text.casefold() if sys.platform == "win32" else text


def command_matches(recorded: str, live: str, adw_id: str) -> bool:
    """Whether a live command line is still the one recorded.

    Its first three words — the program, the script, the first argument — and,
    when the recorded command names the Run's adw_id, that too: an agent's argv
    carries its session dir, so a pid recycled into another Run's agent of the
    same kind fails here. Only these, because the OS reports quoting, paths and
    long prompts differently from the argv that was recorded.
    """
    recorded, live = _comparable(recorded), _comparable(live)
    words = recorded.split()[:3]
    if adw_id and _comparable(adw_id) in recorded:
        words.append(_comparable(adw_id))
    return bool(words) and all(word in live for word in words)


def live_commands(pids) -> dict[int, str]:
    """The current command line of each pid that is running; any other pid is left out."""
    pids = sorted({pid for pid in pids if pid > 0})
    if not pids:
        return {}
    if sys.platform == "win32":
        query = " OR ".join(f"ProcessId={pid}" for pid in pids)
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; ConvertTo-Json -Compress "
                  f"-InputObject @(Get-CimInstance Win32_Process -Filter '{query}' "
                  "| Select-Object ProcessId, CommandLine)")
        done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        if done.returncode != 0:
            raise RuntimeError(f"could not read process command lines: {done.stderr.strip()}")
        rows = json.loads(done.stdout or "[]")
        return {int(row["ProcessId"]): row.get("CommandLine") or "" for row in rows}
    found = {}
    for pid in pids:
        done = subprocess.run(["ps", "-ww", "-o", "stat=", "-o", "args=", "-p", str(pid)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15)
        stat, _, args = done.stdout.strip().partition(" ")
        if done.returncode == 0 and stat and not stat.startswith("Z"):   # a zombie has exited
            found[pid] = args.strip()
    return found


def _running(pid: int) -> bool:
    """Alive and not a zombie: a POSIX child that exited reads alive until reaped."""
    if not pid_alive(pid):
        return False
    if sys.platform == "win32":
        return True
    done = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True, encoding="utf-8", timeout=15)
    return not done.stdout.strip().startswith("Z")


def _wait_until(done, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while not done():
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)
    return True


class NotStoppable(Exception):
    """There is no running Run by that id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _close_trace_as_stopped(conn: sqlite3.Connection, sessions_dir: Path, adw_id: str) -> None:
    """Close a Run's trace as stopped when its own process could not.

    Only for a Run that never settled itself: one hard-killed, or whose process
    was already gone. It writes what the Run's tracer would have — the open
    phase failed, an event saying why, the session `stopped`, no process alive
    — in plain SQL, because this file runs without the ADWs' dependencies.
    """
    now = _now()
    reason = "stopped: the Run was killed before it could settle itself"
    conn.execute("UPDATE phases SET status='fail', ended_at=?, error=COALESCE(error, ?) "
                 "WHERE adw_id=? AND status='running'", (now, reason, adw_id))
    event_id, payload = f"evt_{secrets.token_hex(6)}", {"reason": reason}
    conn.execute("INSERT INTO events (event_id, adw_id, phase_id, parent_id, type, name, "
                 "payload_json, tokens, started_at) VALUES (?,?,'',NULL,'error','stopped',?,0,?)",
                 (event_id, adw_id, json.dumps(payload), now))
    conn.execute("UPDATE sessions SET status='stopped', ended_at=? WHERE adw_id=? "
                 "AND status='running'", (now, adw_id))
    conn.execute("UPDATE processes SET ended_at=? WHERE adw_id=? AND ended_at IS NULL",
                 (now, adw_id))
    events = sessions_dir / adw_id / "events.jsonl"
    if events.parent.is_dir():
        with events.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event_id": event_id, "ts": now, "adw_id": adw_id, "phase_id": "",
                                "type": "error", "name": "stopped", "payload": payload}) + "\n")


def _status(conn: sqlite3.Connection, adw_id: str) -> str | None:
    row = conn.execute("SELECT status FROM sessions WHERE adw_id=?", (adw_id,)).fetchone()
    return row[0] if row else None


def _live_rows(conn: sqlite3.Connection, adw_id: str) -> list[dict]:
    """The Run's processes the trace believes alive: agents first, newest first, then the ADW."""
    rows = conn.execute("SELECT kind, name, pid, command FROM processes WHERE adw_id=? "
                        "AND ended_at IS NULL ORDER BY (kind = 'adw'), id DESC",
                        (adw_id,)).fetchall()
    return [{"kind": kind, "name": name, "pid": pid, "command": command}
            for kind, name, pid, command in rows]


def _check(processes: list[dict], adw_id: str) -> list[dict]:
    """Read each pid's command line now; those still running what was recorded are returned.

    The rest get their outcome: `gone`, or `mismatch` with what the pid runs instead.
    Called again right before every signal, so time spent waiting never lets a
    pid recycled in the meantime be hit.
    """
    live = live_commands(p["pid"] for p in processes)
    verified = []
    for p in processes:
        p["live_command"] = live.get(p["pid"])
        if p["live_command"] is None:
            p["outcome"] = "gone"
        elif command_matches(p["command"], p["live_command"], adw_id):
            verified.append(p)
        else:
            p["outcome"] = "mismatch"
    return verified


def _kill_agents(agents: list[dict], adw_id: str) -> None:
    for p in _check(agents, adw_id):
        kill_tree(p["pid"])
        p["outcome"] = "killed"


def _ask_to_stop(request: Path) -> bool:
    """Leave a stop request for the Run's watcher; whether it was acknowledged in time."""
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text(_now(), encoding="utf-8")
    acknowledged = _wait_until(lambda: not request.exists(), ACK_SECONDS)
    request.unlink(missing_ok=True)           # unheard: never left for a later ADW to find
    return acknowledged


def stop(db_path: str | Path, adw_id: str, grace_seconds: float = GRACE_SECONDS) -> dict:
    """Stop a running Run: its agents first, then the ADW, each pid verified as it is signalled.

    Returns a report: each recorded live process and what became of it
    (`killed`, `stopped`, `gone`, `mismatch`) with a sentence saying so, the
    Run's final status, and whether the Run settled itself or had its trace
    closed for it.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise NotStoppable(f"no trace at {db_path}")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10)
    try:
        status = _status(conn, adw_id)
        if status is None:
            raise NotStoppable(f"no Run {adw_id} in {db_path}")
        if status != "running":
            raise NotStoppable(f"{adw_id} is not running (it is {status})")
        sessions_dir = db_path.parent / "sessions"
        report = _live_rows(conn, adw_id)
        agents = [p for p in report if p["kind"] != "adw"]
        adws = _check([p for p in report if p["kind"] == "adw"], adw_id)

        acknowledged = bool(adws) and _ask_to_stop(sessions_dir / adw_id / STOP_REQUEST)
        _kill_agents(agents, adw_id)             # children before the parent
        for p in adws:
            if sys.platform != "win32" and _check([p], adw_id):
                try:
                    os.kill(p["pid"], signal.SIGTERM)
                    acknowledged = True       # every ADW settles itself on SIGTERM
                except OSError:
                    pass
            pid = p["pid"]
            if acknowledged and _wait_until(lambda: not _running(pid), grace_seconds):
                p["outcome"] = "stopped"
            elif _check([p], adw_id):
                kill_tree(pid)
                p["outcome"] = "killed"
        # An agent the ADW started after the first read (a retried send, say).
        known = {p["pid"] for p in report}
        late = [p for p in _live_rows(conn, adw_id) if p["kind"] != "adw" and p["pid"] not in known]
        _kill_agents(late, adw_id)
        report += late

        settled_by = "run"
        if _status(conn, adw_id) == "running":
            _close_trace_as_stopped(conn, sessions_dir, adw_id)
            settled_by = "stop"
        notes = []
        claimed = conn.execute("SELECT 1 FROM phases WHERE adw_id=? AND name='claim' "
                               "AND status='success'", (adw_id,)).fetchone()
        if claimed and settled_by == "stop":
            notes.append("its issue is still claimed: the Run was killed before it could "
                         "release it, so rerun it with --force")
        for p in report:
            p["said"] = _describe(p)
        return {"adw_id": adw_id, "status": _status(conn, adw_id), "settled_by": settled_by,
                "processes": report, "notes": notes}
    finally:
        conn.close()


def _describe(p: dict) -> str:
    who = f"agent {p['name']}" if p["kind"] != "adw" else "the ADW"
    if p["outcome"] == "mismatch":
        return (f"pid {p['pid']} ({who}) no longer runs `{p['command']}`: it runs "
                f"`{p['live_command']}` now, so it was not signalled")
    if p["outcome"] == "gone":
        return f"{who} (pid {p['pid']}) had already exited"
    return f"{p['outcome']} {who} (pid {p['pid']})"


def main(argv: list[str] | None = None) -> int:
    """The command line. It prints, rather than reporting through `run.console`:
    the Run it stops is another process, and this one has no Run of its own."""
    parser = argparse.ArgumentParser(prog="procs.py", description="Stop a running Run.")
    commands = parser.add_subparsers(dest="command", required=True)
    stopping = commands.add_parser("stop", help="stop a Run: its agents, then the ADW itself")
    stopping.add_argument("adw_id")
    stopping.add_argument("--db", default="adws/adw_data/sssf.db")
    stopping.add_argument("--grace", type=float, default=GRACE_SECONDS,
                          help="seconds the Run gets to settle itself before it is killed")
    stopping.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        report = stop(args.db, args.adw_id, args.grace)
    except NotStoppable as refused:
        print(json.dumps({"error": str(refused)}) if args.json else str(refused))
        return 2
    if args.json:
        print(json.dumps(report))
        return 0
    if not report["processes"]:
        print("no live process was recorded for this Run")
    for p in report["processes"]:
        print(p["said"])
    how = ("it settled itself" if report["settled_by"] == "run"
           else "it could not settle itself, so its trace was closed for it")
    print(f"{report['adw_id']} {report['status']}: {how}")
    for note in report["notes"]:
        print(f"note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
