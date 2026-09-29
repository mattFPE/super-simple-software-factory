"""Agent Skills, offered to an agent only when the roster names them.

A skill is a directory holding a SKILL.md: frontmatter with a `name` and a
`description`, then the instructions (agentskills.io). Both coding agents
could find skills on their own, and neither is allowed to. Found skills
depend on the machine, so runs stop repeating. They include the operator's
tools with reach outside the repo, which permissions.py cannot see, and they
bring workflows of their own that compete with the agent's one job. So pi
runs with `--no-skills` and Claude Code with `--disable-slash-commands`, and
an agent gets exactly the skills in its `skills:` list, the same way on both.

Each is offered the way pi offers its own: name, description and location in
the system prompt, with the instructions read from SKILL.md only when the
task calls for them. That is why a skilled agent needs `read` (or `bash`).

    skills:
      - adws/adw_data/skills/netsuite-sdf-safe-guide/    a skill
      - adws/adw_data/skills/netsuite/                   every skill under it

Keep skills in the repo: then every machine offers the same ones, and a skill
inside the repo is protected like the factory's own code (see protected), so
no agent can rewrite the instructions a later agent will follow.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

import yaml

from .data_types import AgentConfig, SSSFConfig, Skill

SKILL_FILE = "SKILL.md"
READERS = ("read", "bash")                 # a tool that can open SKILL.md

PREAMBLE = """# Skills

The skills below hold instructions for particular kinds of work. Each is listed by name and description only. When your task matches a skill's description, read its SKILL.md in full before you do that part of the work, and follow it. When a skill refers to a relative path, resolve it against the directory its SKILL.md is in. Don't read a skill your task doesn't need.

Where a skill disagrees with the rest of this system prompt, about what to produce, which files you may change, or the shape of your report, the rest of this system prompt wins."""


def resolve(agent: AgentConfig) -> list[Skill]:
    """The agent's skills, in roster order. Raises ValueError on any problem."""
    found = check(agent)
    if found[1]:
        raise ValueError("; ".join(found[1]))
    return found[0]


def check(agent: AgentConfig) -> tuple[list[Skill], list[str]]:
    """Every skill the agent's `skills:` names, and every problem with them."""
    skills: list[Skill] = []
    problems: list[str] = []
    for entry in agent.skills:
        files = _skill_files(Path(entry))
        if files is None:
            problems.append(f"skill {entry!r} not found")
        elif not files:
            problems.append(f"skill {entry!r} holds no {SKILL_FILE}")
        for file in files or []:
            if any(s.file == file.as_posix() for s in skills):
                continue                     # named twice: a directory and a skill inside it
            try:
                skills.append(_read(file))
            except ValueError as error:
                problems.append(str(error))
    seen: dict[str, str] = {}
    for skill in skills:
        if skill.name in seen:
            problems.append(f"two skills named {skill.name!r}: {seen[skill.name]} and {skill.file}")
        seen.setdefault(skill.name, skill.file)
    if skills and agent.tools is not None and not any(t in agent.tools for t in READERS):
        problems.append("has skills but no tool to read them: add `read` to its tools")
    return skills, problems


def _skill_files(path: Path) -> list[Path] | None:
    """SKILL.md files at `path`: itself, its own, or every one below it."""
    if path.is_file():
        return [path.resolve()] if path.name == SKILL_FILE else []
    if not path.is_dir():
        return None
    if (path / SKILL_FILE).is_file():
        return [(path / SKILL_FILE).resolve()]
    return sorted(p.resolve() for p in path.rglob(SKILL_FILE))


def _read(file: Path) -> Skill:
    text = file.read_text(encoding="utf-8").replace("\r\n", "\n")
    meta = {}
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                meta = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError as error:
                raise ValueError(f"{file}: frontmatter is not valid YAML ({error})")
    if not isinstance(meta, dict) or not meta.get("name") or not meta.get("description"):
        raise ValueError(f"{file}: frontmatter needs a `name` and a `description`")
    return Skill(name=str(meta["name"]), description=" ".join(str(meta["description"]).split()),
                 file=file.as_posix())


def prompt_block(skills: list[Skill]) -> str:
    """The system-prompt section that offers `skills`; empty when there are none."""
    if not skills:
        return ""
    lines = [PREAMBLE, "", "<available_skills>"]
    for skill in skills:
        lines += ["  <skill>",
                  f"    <name>{escape(skill.name)}</name>",
                  f"    <description>{escape(skill.description)}</description>",
                  f"    <location>{escape(skill.file)}</location>",
                  "  </skill>"]
    lines.append("</available_skills>")
    return "\n".join(lines)


def protected(cfg: SSSFConfig) -> list[str]:
    """Repo-relative directory prefixes of every skill in the roster.

    A skill is read when an agent needs it, not when the run starts, so a
    builder that edits one rewrites what the reviewer reads next. Skills
    outside the repo can't be policed at all: permissions.py only sees the repo.
    """
    root = Path.cwd().resolve()            # ADWs run from the engineer's checkout
    prefixes = []
    for entry in [*cfg.defaults.skills, *(s for a in cfg.agents for s in a.skills)]:
        path = Path(entry).resolve()
        if path.name == SKILL_FILE:
            path = path.parent
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        prefix = "" if rel == "." else rel.rstrip("/") + "/"
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes
