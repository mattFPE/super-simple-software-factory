"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself,
                               added to what protected() always includes
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import skills
from .data_types import AgentConfig, SSSFConfig

# Protected in every roster; `defaults.protected_files` adds to these, never
# replaces them. The factory's own code, because an agent must not be able to
# edit the machinery that decides whether its work passed. And the repo's CI:
# it grades the PR, and a pushed branch's workflows run with the repo's
# secrets before anyone has reviewed them.
BUILTIN_PROTECTED = ["adws/adw_modules/", "adws/adw_sssf_config/", "adws/adw_*.py", ".github/"]


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout if result.returncode == 0 else ""


def snapshot(run) -> dict[str, str]:
    """Fingerprint every path the working tree currently differs on.

    Tracked files carry their numstat counts, so an edit to an already-dirty
    file still registers as a change. Untracked files are listed by name.
    Gitignored paths never appear, which is why the session runtime under
    `data_dir` — where handoff files legitimately land — needs no special case.
    """
    fingerprints: dict[str, str] = {}
    for line in _git(["diff", "HEAD", "--numstat"], run.repo_root).splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            path = fields[-1].strip()
            fingerprints[path] = f"{fields[0]},{fields[1]}"
    for path in _git(["ls-files", "--others", "--exclude-standard"],
                     run.repo_root).splitlines():
        if path.strip():
            fingerprints[path.strip()] = "untracked"
    return fingerprints


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig) -> list[str]:
    """The session runtime, `{data_dir}/sessions/`, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    Scout writes its findings there, the reviewer its review, the planner its
    plan — a read-only agent is read-only with respect to the REPO, never with
    respect to its own report.

    This is granted from `data_dir` rather than left to .gitignore. The runtime
    is normally ignored, so it never even appears in a snapshot — but an agent's
    ability to record its work must not hang on a gitignore entry that someone
    can delete or that a changed `data_dir` can outgrow.

    Only `sessions/`: the rest of `data_dir` is what agents are TOLD — prompts,
    harness extensions, skills — and is protected (see protected).
    """
    return [_data_dir(cfg) + "sessions/"]


def _data_dir(cfg: SSSFConfig) -> str:
    return cfg.defaults.data_dir.rstrip("/") + "/"


def protected(cfg: SSSFConfig) -> list[str]:
    """Every path no agent may touch unless it names it in its own `writes`.

    The built-ins, the roster's own additions, every skill it names inside the
    repo, and `data_dir` itself outside the session runtime: its prompts and
    harness extensions are what agents are told, and a builder that could
    edit the reviewer's system.md could edit its own grading.
    """
    paths = [*BUILTIN_PROTECTED, *cfg.defaults.protected_files, *skills.protected(cfg),
             _data_dir(cfg)]
    return list(dict.fromkeys(paths))


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """The session runtime; then what is protected; then the agent's own list.

    Only a `writes` entry that NAMES a protected area unlocks it: one inside
    it, like `.github/workflows/` for `.github/`. A broad glob that merely
    matches, like a documenter's `**/*.md`, would otherwise reach every prompt
    and SKILL.md in the repo, since they are all markdown.
    """
    if any(_matches(path, p) for p in always_writable(cfg)):
        return True
    named = [w for w in (agent.writes or []) if _matches(path, w)]
    guards = [q for q in protected(cfg) if _matches(path, q)]
    # Nested areas (a skill inside data_dir) count once, as the outermost:
    # naming anywhere inside it, and matching the path, is naming the area.
    outer = [q for q in guards
             if not any(o != q and _literal(q).startswith(_literal(o)) for o in guards)]
    if outer:
        return all(any(_literal(w).startswith(_literal(q)) for w in named) for q in outer)
    if named:
        return True
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _literal(pattern: str) -> str:
    """The part of a pattern before its first wildcard: the area it names."""
    cut = min((i for i in (pattern.find("*"), pattern.find("?")) if i != -1), default=len(pattern))
    return pattern[:cut]


def _roll_back(run, path: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.
    """
    if path in before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if path not in after else "left as-is (was already modified)"
    if after.get(path) == "untracked":
        try:
            (Path(run.repo_root) / path).unlink()
            return "deleted"
        except OSError as error:
            return f"could not delete ({error})"
    result = subprocess.run(["git", "checkout", "--", path],
                            cwd=run.repo_root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return "rolled back" if result.returncode == 0 else "could not roll back"


def enforce(run, phase, agent: AgentConfig, before: dict[str, str]) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    after = snapshot(run)
    touched = changed_paths(before, after)
    breaches = [p for p in touched if not permitted(p, agent, run.cfg)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(run, p, before, after) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {protected(run.cfg)}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")
