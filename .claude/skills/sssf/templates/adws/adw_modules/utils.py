"""Small shared helpers. Anything bigger belongs in its own module."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def operator_env() -> dict[str, str]:
    """The engineer's own environment, as their shell would hand it over.

    Agents and quality blocks are meant to see exactly what the operator sees:
    their PATH, their toolchains, their globally installed packages. Copying
    os.environ gets almost all the way there — but ADWs launch under `uv run`,
    which prepends its ephemeral venv's bin to PATH and sets VIRTUAL_ENV. That
    venv holds the ADW's OWN dependencies (pydantic, pyyaml), not the
    operator's, so anything a subprocess resolves through it — `python3`,
    `pip`, every globally pip-installed CLI — silently becomes the wrong one.

    Stripping the venv restores parity: `python3` in an agent's bash is the
    same `python3` the engineer gets in their terminal. The ADW's own imports
    are unaffected; this env is only ever handed to child processes.
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", "")
    if not venv:
        return env
    # bin/ on POSIX, Scripts/ on Windows — strip whichever this venv has.
    venv_bins = {os.path.normcase(str(Path(venv) / d)) for d in ("bin", "Scripts")}
    parts = [p for p in env.get("PATH", "").split(os.pathsep)
             if p and os.path.normcase(p.rstrip("\\/")) not in venv_bins]
    env["PATH"] = os.pathsep.join(parts)
    return env


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


def new_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_prompt(arg: str) -> str:
    """CLI prompt arg: an issue (`#42`, or its URL) resolves to the issue as a
    request, a file path to its contents, anything else is inline text."""
    from . import issues                  # here: issues imports this module
    if issues.parse_ref(arg):
        issue = issues.load(arg)
        issues.require_ready(issue)       # untrusted text never reaches an agent
        return issues.as_prompt(issue)
    try:
        p = Path(arg)
        if p.is_file():
            return p.read_text()
    except OSError:
        pass
    return arg


def engineer_name() -> str:
    name = os.environ.get("ENGINEER_NAME", "").strip()
    if name:
        return name
    try:
        out = subprocess.run(["git", "config", "user.name"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except OSError:
        pass
    return os.environ.get("USER", "engineer")
