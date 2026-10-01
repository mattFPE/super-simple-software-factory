"""Child-process control for coding agents: idle watchdog and tree kill.

A coding agent that hangs does not fail — it goes quiet. No events, no tokens,
an ADW blocked forever on a read. And killing it is not one pid: pi's Windows
launcher runs the real pi as a child, and agents spawn shells and subagents of
their own, so killing the pid you started orphans the process doing the work.
Both adapters run their child inside `supervise()`, which kills the whole tree
when the agent goes silent too long, or when the ADW itself is leaving early.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager


def popen_kwargs() -> dict:
    """Extra Popen kwargs that make the child's tree killable as one unit.

    POSIX: a new session makes the child a process-group leader, so killpg
    reaches every descendant. (It also stops a terminal Ctrl+C reaching the
    child directly — `supervise()` kills the tree on the way out instead.)
    Windows needs nothing: `taskkill /T` walks the tree by parent pid.
    """
    return {} if sys.platform == "win32" else {"start_new_session": True}


def kill_tree(pid: int) -> None:
    """Kill a process and every descendant. Never raises."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


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
