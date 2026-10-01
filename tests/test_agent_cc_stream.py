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
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "sssf"
sys.path.insert(0, str(SKILL / "templates" / "adws"))
from adw_modules import agent_cc  # noqa: E402
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


def fake_claude(directory: Path, events: list[dict]) -> Path:
    """An executable `claude` that drains stdin and prints `events` as stream-json."""
    stream = directory / "stream.jsonl"
    stream.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    script = directory / "fake_claude.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write(open({str(stream)!r}, encoding='utf-8').read())\n",
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


if __name__ == "__main__":
    unittest.main()
