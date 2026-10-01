"""Claude Code interface — the second coding agent, beside pi.

Runs `claude -p --output-format stream-json --verbose` and tails its JSONL
stdout line by line, forwarding each event to a callback WHILE the agent works,
exactly like `agent_pi`. It takes the same `PiRequest` and returns the same
`PiResult`, so `agents.execute` cannot tell the two apart.

Where Claude Code differs from pi, and what this module does about it:

- **Sessions are create XOR continue.** `--session-id` only creates (a second
  use fails "already in use") and `--resume` only continues, and both demand a
  UUID. The SSSF session id is mapped to a stable uuid5, and a small state file
  in `session_dir` records that the session exists — so running and continuing
  an agent are still one call to the caller.
- **Cost is cumulative on resume.** `total_cost_usd` and `modelUsage` report
  the whole session, not this invocation. The state file keeps the last
  totals so each send is billed its delta — a retried phase is not billed twice.
- **Tool names differ.** The roster speaks pi's names (`read`, `bash`, ...);
  they are translated here. Names Claude Code does not recognise are dropped by
  the CLI itself, which is why `bash` asks for both `Bash` and `PowerShell`:
  on Windows only the latter exists.
- **Harness extensions are plugin directories.** pi's `-e file.ts` has no
  equivalent; a directory in `harness_engineering` is loaded with
  `--plugin-dir`, and anything else fails `preflight`.
- **The host session leaks.** An ADW launched from inside Claude Code inherits
  that session's env (CLAUDECODE, its session id, its messaging socket); those
  are stripped so the child is a fresh session, not a confused sibling.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import procs
from .agent_pi import _clip, _label, ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS
from .data_types import PiRequest, PiResult
from .utils import now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_CODE_PATH", "claude")

MODEL_ALIASES = ("fable", "opus", "sonnet", "haiku", "opusplan")

# Env the parent Claude Code session sets for ITS children. Inherited, they make
# the child think it is a subagent of the host rather than a session of its own.
HOST_SESSION_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
)

# pi tool name -> Claude Code built-ins. A name not listed passes through as-is,
# so a roster may also name Claude Code tools directly (`WebFetch`, `NotebookEdit`).
TOOL_NAMES: dict[str, list[str]] = {
    "read": ["Read"],
    "bash": ["Bash", "PowerShell"],
    "edit": ["Edit"],
    "write": ["Write"],
    "grep": ["Grep"],
    "find": ["Glob"],
    "ls": ["Glob"],
    # subagents.ts's four tools are one native tool here
    "subagent_create": ["Agent"],
    "subagent_continue": ["Agent"],
    "subagent_list": ["Agent"],
    "subagent_remove": ["Agent"],
}

# The roster's thinking scale -> `--effort`. Claude Code has no "off"; low is its floor.
EFFORT = {"off": "low", "minimal": "low", "low": "low", "medium": "medium",
          "high": "high", "xhigh": "xhigh", "max": "max"}


def resolve_model(pattern: str) -> tuple[str, str]:
    """Resolve a roster model to ``("anthropic", model)`` for `--model`.

    Accepts an alias (`opus`, `sonnet`, ...), a full `claude-*` id, or either
    with an `anthropic/` prefix. Claude Code has no offline catalog to check
    the id against, so a well-formed but unknown id fails at the first call.
    """
    model = pattern.removeprefix("anthropic/")
    base = model.removesuffix("[1m]")
    if base in MODEL_ALIASES or base.startswith("claude-"):
        return "anthropic", model
    raise ValueError(f"model {pattern!r} is not a Claude model — claude_code takes an "
                     f"alias ({', '.join(MODEL_ALIASES)}) or a claude-* id")


def preflight(extensions: list[str]) -> list[str]:
    """Problems that would stop a claude_code agent before it spawns."""
    problems = []
    if not shutil.which(CLAUDE_PATH):
        problems.append(f"claude not found on PATH ({CLAUDE_PATH!r}) — install Claude Code "
                        "or set CLAUDE_CODE_PATH")
    for extension in extensions:
        if not Path(extension).is_dir():
            problems.append(f"harness_engineering {extension!r} is not a Claude Code plugin "
                            "directory — pi extensions (.ts) do not load in claude_code")
    return problems


def tool_names(tools: list[str]) -> list[str]:
    """Translate a roster tool list into Claude Code built-in names, deduplicated."""
    names: list[str] = []
    for tool in tools:
        for name in TOOL_NAMES.get(tool, [tool]):
            if name not in names:
                names.append(name)
    return names


def session_uuid(session_id: str) -> str:
    """The stable UUID Claude Code knows this SSSF session by."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sssf:{session_id}"))


def _child_env() -> dict[str, str]:
    env = operator_env()
    for key in HOST_SESSION_ENV:
        env.pop(key, None)
    return env


def _message(event: dict) -> dict:
    """The assistant/user message envelope. Other events can carry `message`
    as a plain string (a `permission_denied` system event does), so anything
    that isn't an object reads as no message at all."""
    message = event.get("message")
    return message if isinstance(message, dict) else {}


def _text_of(content) -> str:
    """Claude Code content is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or []
                   if isinstance(part, dict) and part.get("type") == "text")


class ToolCallTracker:
    """Folds Claude Code's tool stream into ONE normalized record per call.

    A call is announced as a `tool_use` block in an assistant message and
    answered by a `tool_result` block in the following user message, matched on
    the tool_use id. The record has the same shape `agent_pi.ToolCallTracker`
    emits, so the tracer and the UI read both agents alike.
    """

    def __init__(self) -> None:
        self._open: dict[str, dict] = {}

    def observe(self, event: dict) -> Optional[dict]:
        """Returns the record for a finished tool call, else None."""
        etype = event.get("type")
        content = _message(event).get("content")
        if etype not in ("assistant", "user") or not isinstance(content, list):
            return None
        if etype == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._open.setdefault(str(block.get("id")), {
                        "tool": block.get("name") or "tool",
                        "args": block.get("input") or {},
                        "started_at": now_iso(),
                        "clock": time.monotonic(),
                    })
            return None
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # A user message carries one result per event in practice.
                return self._finish(block, event.get("parent_tool_use_id"))
        return None

    def _finish(self, block: dict, parent: Optional[str]) -> dict:
        call_id = str(block.get("tool_use_id") or "")
        opened = self._open.pop(call_id, {})
        tool = str(opened.get("tool") or "tool")
        args = opened.get("args") or {}
        record = {
            "tool": tool,
            "tool_call_id": call_id,
            "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": not block.get("is_error", False),
            "label": _label(tool, args),
        }
        if parent:
            record["parent_tool_call_id"] = parent   # a subagent's call
        result_text = _text_of(block.get("content"))
        if result_text:
            record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
        record["ended_at"] = now_iso()
        if opened.get("clock"):
            record["duration_ms"] = int((time.monotonic() - opened["clock"]) * 1000)
        if opened.get("started_at"):
            record["started_at"] = opened["started_at"]
        return record


def _occupancy(usage: dict) -> int:
    return int(sum(usage.get(part) or 0 for part in
                   ("input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens")))


def _bill(result: PiResult, model_usage: dict, previous: dict) -> None:
    """Fold this send's share of the session's cumulative `modelUsage` into
    `result`, in pi's usage shape so `UsageBreakdown` stays one-for-one."""
    for model, now in model_usage.items():
        before = previous.get(model, {})

        def delta(key: str):
            return (now.get(key) or 0) - (before.get(key) or 0)

        usage = {"input": delta("inputTokens"), "output": delta("outputTokens"),
                 "cacheRead": delta("cacheReadInputTokens"),
                 "cacheWrite": delta("cacheCreationInputTokens"),
                 "reasoning": delta("thinkingTokens"),
                 "cost": {"total": delta("costUSD")}}
        total = usage["input"] + usage["output"] + usage["cacheRead"] + usage["cacheWrite"]
        result.usage.add_turn(usage, total)
        result.tokens += total
        result.cost += usage["cost"]["total"]


def run(request: PiRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive Claude Code turn — create or continue the session.

    `on_spawn(pid)` and `on_exit(pid)` bracket the child process so the caller
    can record it as killable, as with pi.
    """
    _, model = resolve_model(request.model)
    claude_uuid = session_uuid(request.session_id)
    session_dir = Path(request.session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    state_path = session_dir / f"{claude_uuid}.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None

    # The system prompt goes by file: Windows caps a command line at 32K chars.
    system_path = session_dir / f"{claude_uuid}.system.md"
    system_path.write_text(request.system_prompt, encoding="utf-8")

    cmd = [
        shutil.which(CLAUDE_PATH) or CLAUDE_PATH,
        "-p", "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--effort", EFFORT.get(request.thinking, "medium"),
        "--system-prompt-file", str(system_path),
        # SSSF, not Claude Code, is the permission boundary: tools are limited
        # by --tools and writes are enforced after the call by permissions.py.
        # Nobody is there to answer a prompt, so there must be none to answer.
        "--permission-mode", "bypassPermissions",
        # The operator's MCP servers (production connectors included) and
        # skills are theirs, not the agent's. The skills the roster names are
        # offered in the system prompt instead (skills.py), as for pi.
        "--strict-mcp-config", "--disable-slash-commands",
    ]
    cmd += ["--resume", claude_uuid] if state is not None else ["--session-id", claude_uuid]
    if request.tools is not None:
        cmd += ["--tools", ",".join(tool_names(request.tools))]
    for extension in request.extensions:
        cmd += ["--plugin-dir", extension]

    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = raw_path.with_name(raw_path.stem + ".stderr.log")

    result = PiResult(session_id=request.session_id)
    final: dict = {}
    last_turn: dict = {}
    session_model = model                    # an alias until init names the real id
    # The prompt travels on stdin, written whole and closed: argv has the same
    # 32K ceiling, and a closed stdin is what keeps the child from waiting on
    # input that never arrives (see agent_pi). stderr goes to a file so a
    # chatty child cannot fill a pipe nobody is reading and deadlock the tail.
    with stderr_path.open("a", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=stderr_file, text=True, encoding="utf-8",
                                   errors="replace", bufsize=1, cwd=request.cwd,
                                   env=_child_env(), **procs.popen_kwargs())
        if on_spawn:
            on_spawn(process.pid)
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(request.prompt)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass                             # it died early; stderr says why
        with raw_path.open("a", encoding="utf-8") as raw,                 procs.supervise(process, request.idle_timeout_seconds,
                                f"claude {model}") as watchdog:
            for line in process.stdout:
                watchdog.touch()
                raw.write(line)
                raw.flush()                  # events land on disk as they happen
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init":
                    session_model = event.get("model") or session_model
                    if state is None:
                        # The session exists from here on: continue it next
                        # time, even if this run dies before its result.
                        state = {"model_usage": {}}
                        state_path.write_text(json.dumps(state), encoding="utf-8")
                elif etype == "assistant" and not event.get("parent_tool_use_id"):
                    last_turn = _message(event).get("usage") or last_turn
                    text = _text_of(_message(event).get("content"))
                    if text:
                        result.text = text   # superseded by `result`, kept if none comes
                elif etype == "result":
                    final = event
                if on_event:
                    on_event(event)
        result.returncode = process.wait()
    if on_exit:
        on_exit(process.pid)

    if final:
        if isinstance(final.get("result"), str):
            result.text = final["result"]
        model_usage = final.get("modelUsage") or {}
        _bill(result, model_usage, (state or {}).get("model_usage", {}))
        state_path.write_text(json.dumps({"model_usage": model_usage}), encoding="utf-8")
        iterations = (final.get("usage") or {}).get("iterations") or []
        result.context_tokens = _occupancy(iterations[-1] if iterations else last_turn)
        result.context_window = int((model_usage.get(session_model) or {})
                                    .get("contextWindow") or 0)
        if final.get("is_error"):
            raise RuntimeError(f"claude {final.get('subtype', 'error')}: "
                               f"{str(final.get('result') or final.get('errors') or '')[-800:]}")
    if result.returncode != 0 and not result.text:
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(f"claude exited {result.returncode}: {stderr.strip()[-800:]}")
    return result
