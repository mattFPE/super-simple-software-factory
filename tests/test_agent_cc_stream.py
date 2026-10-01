"""The Claude Code driver survives every event its stream can carry.

Drives agent_cc.run's real read loop against a fake `claude` that replays a
recorded stream, with the tool tracker attached the way agents._event_forwarder
attaches it.

Run: uv run --with pydantic --with python-dotenv --with pyyaml --with rich \
         python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
sys.path.insert(0, str(SKILL / "templates" / "adws"))
from adw_modules import agent_cc, procs  # noqa: E402
from adw_modules.data_types import PiRequest  # noqa: E402

TOOL_USE_ID = "toolu_01UDETjJTY5AA2ze4AjjV18o"

# A builder's PowerShell call blocked by Claude Code's safety check, as it
# arrived in a real raw_output.jsonl (#3): the `permission_denied` system event
# carries `message` as a STRING, unlike assistant/user events.
BLOCKED_CALL = [
    {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "session_id": "s"},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": TOOL_USE_ID, "name": "PowerShell",
         "input": {"command": "Remove-Item -Recurse //"}}]}},
    {"type": "system", "subtype": "permission_denied", "tool_name": "PowerShell",
     "tool_use_id": TOOL_USE_ID, "decision_reason_type": "safetyCheck",
     "decision_reason": "Removal targets a protected system path",
     "message": "Remove-Item on system path '//' is blocked. This path is protected from removal.",
     "uuid": "u", "session_id": "s"},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": TOOL_USE_ID, "is_error": True,
         "content": "Remove-Item on system path '//' is blocked."}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "done"},
]


def fake_claude(directory: Path, events: list[dict], stall_first: int = 0,
                pause_after_init: float = 0) -> Path:
    """An executable `claude` that drains stdin and prints `events` as stream-json.

    Its first `stall_first` launches instead start a SessionStart hook and then
    go silent for good — Claude Code stuck in its hook step on --resume (#6).
    `pause_after_init` goes silent that long once the session has started, the
    way an agent thinks. Every launch is counted in `launches` beside it.
    """
    stream = directory / "stream.jsonl"
    stream.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    script = directory / "fake_claude.py"
    script.write_text(
        "import json, pathlib, sys, time\n"
        f"count = pathlib.Path({str(directory / 'launches')!r})\n"
        "n = int(count.read_text()) if count.exists() else 0\n"
        "count.write_text(str(n + 1))\n"
        "sys.stdin.read()\n"
        f"if n < {stall_first}:\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'hook_started',\n"
        "                      'hook_name': 'SessionStart:resume'}), flush=True)\n"
        "    time.sleep(3600)\n"
        f"lines = open({str(stream)!r}, encoding='utf-8').readlines()\n"
        "sys.stdout.write(lines[0])\n"
        "sys.stdout.flush()\n"
        f"time.sleep({pause_after_init})\n"
        "sys.stdout.write(''.join(lines[1:]))\n",
        encoding="utf-8")
    if os.name == "nt":
        launcher = directory / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "claude"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


class BlockedToolCall(unittest.TestCase):
    def test_a_permission_denied_event_does_not_kill_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            original = agent_cc.CLAUDE_PATH
            agent_cc.CLAUDE_PATH = str(fake_claude(tmp, BLOCKED_CALL))
            self.addCleanup(setattr, agent_cc, "CLAUDE_PATH", original)

            tracker = agent_cc.ToolCallTracker()
            records = []

            def forward(event):          # agents._event_forwarder, minus the tracer
                record = tracker.observe(event)
                if record is not None:
                    records.append(record)

            result = agent_cc.run(PiRequest(
                prompt="build it", system_prompt="you build", model="sonnet",
                session_id="sess", session_dir=str(tmp / "session"),
                raw_output_path=str(tmp / "session" / "raw_output.jsonl"),
                cwd=str(tmp), idle_timeout_seconds=30), on_event=forward)

            self.assertEqual(result.text, "done")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["tool"], "PowerShell")
            self.assertEqual(records[0]["tool_call_id"], TOOL_USE_ID)
            self.assertFalse(records[0]["ok"])


class StalledStart(unittest.TestCase):
    """A send that stalls before its session starts is repeated, once (#6)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, value in (("CLAUDE_PATH", None), ("START_TIMEOUT_SECONDS", 2)):
            self.addCleanup(setattr, agent_cc, name, getattr(agent_cc, name))
            if value is not None:
                setattr(agent_cc, name, value)

    def send(self, stall_first=0, pause_after_init=0.0):
        agent_cc.CLAUDE_PATH = str(fake_claude(self.tmp, BLOCKED_CALL, stall_first,
                                               pause_after_init))
        return agent_cc.run(PiRequest(
            prompt="build it", system_prompt="you build", model="sonnet",
            session_id="sess", session_dir=str(self.tmp / "session"),
            raw_output_path=str(self.tmp / "session" / "raw_output.jsonl"),
            cwd=str(self.tmp), idle_timeout_seconds=20))

    def launches(self) -> int:
        return int((self.tmp / "launches").read_text())

    def test_a_send_that_stalls_before_init_is_repeated_not_failed(self):
        started = time.monotonic()
        self.assertEqual(self.send(stall_first=1).text, "done")
        self.assertEqual(self.launches(), 2)
        self.assertLess(time.monotonic() - started, 15, "waited out the idle window")

    def test_a_send_that_stalls_twice_fails_as_a_stalled_start(self):
        with self.assertRaisesRegex(procs.StalledStart, "did not start a session within 2s"):
            self.send(stall_first=2)
        self.assertEqual(self.launches(), 2)

    def test_silence_after_init_gets_the_whole_idle_window(self):
        self.assertEqual(self.send(pause_after_init=4).text, "done")
        self.assertEqual(self.launches(), 1)


if __name__ == "__main__":
    unittest.main()
