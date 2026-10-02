"""Config loading/validation and agent execution.

Every ADW validates its agents before running (fail fast, nothing spawns
against a half-valid config). Every agent call parses against a concrete
output type; parse failures and gate violations re-prompt the SAME session
with a correction — context intact, bounded retries. Agent proposes, code
disposes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import yaml

from . import agent_cc, agent_pi, permissions, procs, prompts, quality as quality_blocks, skills
from .data_types import (AgentCall, AgentConfig, EnvelopeBase, EventRecord,
                         GateCheck, GateReport, Phase, PiRequest, PiResult, SSSFConfig,
                         UsageBreakdown)
from .utils import new_id

JSON_FIX_ATTEMPTS = 2      # continue-with-correction attempts for malformed JSON

# coding_agent -> its interface. Each takes a PiRequest and returns a PiResult,
# and exposes resolve_model() and a ToolCallTracker, so execute() is agnostic.
INTERFACES = {"pi": agent_pi, "claude_code": agent_cc}


class GateFailure(RuntimeError):
    pass


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: str = "adws/adw_sssf_config/sssf.config.yaml") -> SSSFConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults", {}) or {}
    for agent in raw.get("agents", []) or []:
        for key in ("coding_agent", "model", "thinking", "color", "tools", "writes",
                    "idle_timeout_seconds"):
            if key in defaults:
                agent.setdefault(key, defaults[key])
        agent.setdefault("harness_engineering", defaults.get("harness_engineering", []))
        agent.setdefault("skills", defaults.get("skills", []))
    cfg = SSSFConfig(**raw)
    for agent in cfg.agents:
        _settle_model(agent)
    return cfg


def _settle_model(agent: AgentConfig) -> None:
    """Collapse `model:` candidates to the first one that resolves here.

    Which provider name a model sits under depends on the machine — a ChatGPT
    login registers `openai-codex/…`, an API key `openai/…` — so a committed
    roster may list both. None resolving leaves the first, for validate() to
    report along with every candidate it tried.
    """
    candidates = [agent.model] if isinstance(agent.model, str) else list(agent.model)
    agent.model_candidates = candidates
    for candidate in candidates:
        try:
            _interface(agent).resolve_model(candidate)
        except ValueError:
            continue
        agent.model = candidate
        return
    agent.model = candidates[0]


def resolve(cfg: SSSFConfig, name: str) -> AgentConfig:
    for agent in cfg.agents:
        if agent.name == name:
            return agent
    raise SystemExit(f"agent {name!r} is not defined in the config — "
                     f"available: {[a.name for a in cfg.agents]}")


def validate(cfg: SSSFConfig, required: list[str], quality: list[str] = ()) -> None:
    """Fail fast: every required agent must be usable, and every quality block
    the ADW runs (`REQUIRED_QUALITY`) must have a command that resolves."""
    problems = quality_blocks.preflight(cfg, list(quality))
    for name in required:
        try:
            agent = resolve(cfg, name)
        except SystemExit as e:
            problems.append(str(e))
            continue
        for label, ref in (("system", agent.prompt_engineering.system),
                           ("user", agent.prompt_engineering.user)):
            if not Path(ref).is_file():
                problems.append(f"agent {name!r}: {label} prompt not found: {ref}")
        try:
            _interface(agent).resolve_model(agent.model)
        except ValueError as e:
            tried = agent.model_candidates
            problems.append(f"agent {name!r}: " + (
                f"none of its {len(tried)} model candidates resolve here: {', '.join(tried)}"
                if len(tried) > 1 else str(e)))
        if agent.coding_agent == "claude_code":
            problems += [f"agent {name!r}: {p}"
                         for p in agent_cc.preflight(agent.harness_engineering)]
        problems += [f"agent {name!r}: {p}" for p in skills.check(agent)[1]]
    if problems:
        raise SystemExit("config validation failed:\n- " + "\n- ".join(problems))


# ── execution ────────────────────────────────────────────────────────────────

def execute(run, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> coding agent run -> typed parse -> gates -> envelope."""
    agent = resolve(run.cfg, phase.params.owner)
    agent_dir = run.session_dir / agent.name
    agent_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "prompt": call.prompt,
        "previous_envelope": call.previous.model_dump_json(indent=2) if call.previous else "(none)",
        "context_handoff_dir": str(run.context_handoff_dir),
    }
    system_text = prompts.render(agent.prompt_engineering.system, variables)
    offered = skills.resolve(agent)       # validate() checked them; read again, as they are now
    if offered:                           # in the saved system.md too: the trace shows what it was offered
        system_text = f"{system_text.rstrip()}\n\n{skills.prompt_block(offered)}\n"
    user_text = prompts.render(agent.prompt_engineering.user, variables)
    prompts.save(agent_dir / "prompts", "system.md", system_text)
    prompts.save(agent_dir / "prompts", "user.md", user_text)
    drift = contract_drift(user_text, call.output_type)
    if drift:   # before the first send: a drifted contract costs no tokens to find
        raise RuntimeError(
            f"output contract drift for {agent.name} ({call.output_type.__name__}) — the "
            "user.md ## Report example and the type disagree (SKILL.md rule 2):\n- "
            + "\n- ".join(drift))

    session_id = _agent_session_id(run, agent)
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_start", name=agent.name,
                                 payload={"model": agent.model, "thinking": agent.thinking,
                                          "color": agent.color,
                                          "session_id": session_id,
                                          "coding_agent": agent.coding_agent,
                                          "purpose": agent.purpose,
                                          "tools": agent.tools,  # None = all tools
                                          "harness_engineering": agent.harness_engineering,
                                          "skills": [s.name for s in offered]}))
    run.console.agent_started(agent.name, agent.model, session_id)

    # Parse retries and gate corrections re-enter the SAME session, so the
    # last send is the one whose context occupancy is current — while spend is
    # the opposite: every send costs, so usage accumulates across all of them.
    latest: PiResult | None = None
    spent = UsageBreakdown()

    def send(prompt_text: str) -> PiResult:
        nonlocal latest
        request = PiRequest(
            prompt=prompt_text,
            system_prompt=system_text,
            model=agent.model,
            thinking=agent.thinking,
            session_id=session_id,
            # absolute: these are read by the agent subprocess, which runs in repo_root
            session_dir=str((agent_dir / f"{agent.coding_agent}_sessions").resolve()),
            raw_output_path=str((agent_dir / "raw_output.jsonl").resolve()),
            tools=agent.tools,
            # absolute, like the paths above: they live in THIS checkout, and the
            # agent's cwd may be a worktree where a relative path means something else
            extensions=[str(Path(e).resolve()) for e in agent.harness_engineering],
            cwd=str(run.repo_root),
            idle_timeout_seconds=agent.idle_timeout_seconds,
        )
        result = _interface(agent).run(
            request,
            on_event=_event_forwarder(run, phase, agent),
            # The argv it really runs, so a stop can check the pid still runs it.
            on_spawn=lambda pid, argv: run.tracer.process_start(
                run.adw_id, "agent", agent.name, pid, procs.recorded_command(argv)),
            on_exit=lambda pid: run.tracer.process_end(run.adw_id, pid))
        run.add_usage(result.tokens, result.cost)
        spent.merge(result.usage)
        latest = result
        return result

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections — is
    # measured against this one baseline.
    tree_before = permissions.snapshot(run)

    result = send(user_text)
    envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # claim gates — violations flow back into the SAME session as corrections
    for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
        violations = []
        for gate in call.gates:
            report = _as_report(gate(envelope, run))
            found = report.violations
            run.tracer.gate_row(phase, gate.__name__, report, gate_attempt)
            run.tracer.event(EventRecord(
                adw_id=run.adw_id, phase_id=phase.phase_id,
                type="gate_fail" if found else "gate_pass", name=gate.__name__,
                payload={"attempt": gate_attempt, "violations": found,
                         "checks": [c.model_dump() for c in report.checks]}))
            run.console.gate_result(gate.__name__, report)
            violations.extend(found)
        if not violations:
            break
        if gate_attempt > phase.params.retries:
            raise GateFailure(f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                              + "\n- ".join(violations))
        phase.attempt = gate_attempt
        run.console.retry(agent.name, gate_attempt, phase.params.retries,
                          f"{len(violations)} gate violation(s)")
        correction = ("Your previous response failed validation:\n- "
                      + "\n- ".join(violations)
                      + "\n\nFix these problems, then re-emit ONLY your Report JSON.")
        result = send(correction)
        envelope, attempt = _parse_with_retries(run, phase, call, result, send)

    # Permission is checked after every send is done, and before the envelope is
    # accepted: an agent does not get to report success on a phase in which it
    # wrote somewhere it was not allowed to.
    try:
        touched = permissions.enforce(run, phase, agent, tree_before)
    except permissions.PermissionBreach as breach:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="error", name="permission_breach",
                                     payload={"agent": agent.name, "error": str(breach),
                                              "writes": agent.writes,
                                              "protected_files": permissions.protected(run.cfg)}))
        raise
    if touched:
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="log", name="paths_touched",
                                     payload={"agent": agent.name, "paths": touched}))

    _persist_envelope(run, phase, agent.name, call, envelope, attempt, valid=True)
    run.console.envelope_summary(envelope)
    context = latest or result
    run.tracer.agent_session_row(run.adw_id, agent, session_id,
                                 context_tokens=context.context_tokens,
                                 context_window=context.context_window)
    run.save_agent_map(agent.name, {"session_id": session_id, "model": agent.model,
                                    "coding_agent": agent.coding_agent})
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="handoff", name=agent.name,
                                 payload={"artifacts": envelope.artifacts,
                                          "summary": envelope.summary}))
    run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                 type="agent_end", name=agent.name,
                                 # Phase totals, not the last send's: a retried
                                 # phase paid for every attempt.
                                 tokens=spent.total_tokens,
                                 payload={"cost": spent.total_cost,
                                          "usage": spent.model_dump(),
                                          "context_tokens": context.context_tokens,
                                          "context_window": context.context_window}))
    run.console.agent_finished(agent.name, spent.total_tokens, spent.total_cost)
    if envelope.status != "success":
        raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
    return envelope


def contract_drift(user_text: str, output_type: type[EnvelopeBase]) -> list[str]:
    """Where a prompt's `## Report` example and its output type disagree.

    Every field has a default, so drift never fails parsing — it fails SILENTLY:
    a key the type lacks is dropped, a field the prompt never asks for arrives
    as its default, and a gate then checks an empty list. So compare the keys,
    both ways, whenever the Report says it produces THIS type ("matching
    `Type`"). The four EnvelopeBase fields are shared and may be omitted. A
    Report naming another type (adw_prompt reuses an agent for GenericOutput)
    or none at all is not this call's contract, and is not checked.
    """
    heading = user_text.find("## Report")
    if heading == -1:
        return []
    report = user_text[heading:]
    named = re.search(r"matching `(\w+)`", report)
    if not named or named.group(1) != output_type.__name__:
        return []
    block = re.search(r"```json\s*(\{.*?\})\s*```", report, re.S)
    if not block:
        return [f"the Report section names {output_type.__name__} but shows no ```json example"]
    try:
        example = set(json.loads(block.group(1)))
    except json.JSONDecodeError as error:
        return [f"the Report JSON example is not valid JSON ({error})"]
    fields = set(output_type.model_fields)
    own = fields - set(EnvelopeBase.model_fields)
    problems = [f"example key {key!r} is not a {output_type.__name__} field — the agent's "
                "value would be silently dropped" for key in sorted(example - fields)]
    problems += [f"{output_type.__name__}.{key} is never asked for in the example — it would "
                 "always arrive as its default" for key in sorted(own - example)]
    return problems


# ── internals ────────────────────────────────────────────────────────────────

def _as_report(result) -> GateReport:
    """Accept a GateReport, or a legacy gate that returned a violations list."""
    if isinstance(result, GateReport):
        return result
    return GateReport(checks=[GateCheck(item=str(v), ok=False) for v in (result or [])])


def _interface(agent: AgentConfig):
    return INTERFACES[agent.coding_agent]


def _agent_session_id(run, agent: AgentConfig) -> str:
    entry = run.agent_map.get(agent.name)
    if entry and entry.get("model") == agent.model:
        return entry["session_id"]           # rejoin the existing context window
    return f"sssf-{run.adw_id}-{agent.name}-{new_id(4)}"


def _event_forwarder(run, phase: Phase, agent: AgentConfig):
    """One tool_call event per real tool call, with its exact args and result."""
    tracker = _interface(agent).ToolCallTracker()
    agent_name = agent.name

    def forward(event: dict) -> None:
        record = tracker.observe(event)
        if record is None:
            return
        # The call's span rides the columns; duration_ms stays in the payload.
        run.tracer.event(EventRecord(adw_id=run.adw_id, phase_id=phase.phase_id,
                                     type="tool_call", name=record.pop("label"),
                                     started_at=record.pop("started_at", None),
                                     ended_at=record.pop("ended_at", None),
                                     payload={**record, "agent": agent_name}))
    return forward


def _extract_json(text: str) -> dict:
    candidate = text
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.removeprefix("json").strip()
            if block.startswith("{"):
                candidate = block
                break
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the response")
    return json.loads(candidate[start:end + 1])


def _parse_with_retries(run, phase: Phase, call: AgentCall, result, send):
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded)."""
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            payload = _extract_json(result.text)
            return call.output_type.model_validate(payload), attempt
        except Exception as error:
            _persist_envelope(run, phase, phase.params.owner, call, None, attempt,
                              valid=False, raw=result.text)
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}") from error
            run.console.retry(phase.params.owner, attempt, JSON_FIX_ATTEMPTS,
                              f"invalid {call.output_type.__name__} JSON: {error}")
            fields = ", ".join(call.output_type.model_fields.keys())
            result = send(
                f"Your response was not valid JSON for the required structure "
                f"({error}). Respond again with ONLY a JSON object with these "
                f"fields: {fields}. No prose, no code fences.")


def _persist_envelope(run, phase: Phase, agent_name: str, call: AgentCall,
                      envelope: Optional[EnvelopeBase], attempt: int,
                      valid: bool, raw: str = "") -> None:
    payload_json = envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    run.tracer.envelope_row(phase, agent_name, call.output_type.__name__,
                            payload_json, valid, attempt)
    if envelope:
        record = {"agent_name": agent_name, "purpose": resolve(run.cfg, agent_name).purpose,
                  "output_type": call.output_type.__name__, "attempt": attempt,
                  **envelope.model_dump()}
        (run.session_dir / agent_name / "envelope.json").write_text(json.dumps(record, indent=2),
                                                                    encoding="utf-8")
