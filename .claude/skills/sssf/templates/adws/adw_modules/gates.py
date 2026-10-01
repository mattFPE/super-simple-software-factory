"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import git_helper, issues, quality
from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _path(run, declared: str) -> Path:
    """Where a path an agent declared actually is.

    Absolute stays as-is. Relative is the repo's view — `run.repo_root`, which
    is the run's worktree when it has one — falling back to this process's cwd,
    where the session folder lives, for a report the agent named relatively.
    """
    p = Path(declared)
    if p.is_absolute():
        return p
    in_repo = Path(run.repo_root) / p
    return in_repo if in_repo.exists() or not p.exists() else p


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _path(run, a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _path(run, a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _path(run, a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text(encoding="utf-8"))
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must exist on disk."""
    report = GateReport()
    for f in getattr(envelope, "changed_files", []):
        p = _path(run, f)
        report.check(f, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "claimed changed file does not exist")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_fail_without_change(envelope: EnvelopeBase, run) -> GateReport:
    """Test-first, checked: the builder's new tests must fail without its change.

    Whether a test was written first leaves no trace in the tree. What it buys
    does: a test that fails on the old code and passes on the new one measures
    the change. So every changed file the builder did NOT declare a test is put
    back to HEAD, the suite runs, and it has to fail; then the files come back.
    A tautological test, or one that never reaches the new code, passes here —
    and that is the violation. Passing WITH the change is the test phase's job.

    A change with no new behaviour (a prefactor, docs, config) declares no test
    files and says why in `no_new_tests_reason`; the check is skipped, and the
    reason travels to the PR. Only in a worktree: in the engineer's checkout,
    putting files back could overwrite edits they are making while it runs.
    """
    report = GateReport()
    tests = [_repo_path(run, t) for t in getattr(envelope, "test_files", [])]
    reason = getattr(envelope, "no_new_tests_reason", "").strip()
    if not tests:
        if reason:
            run.report["tests"] = f"**No new tests:** {reason}"
        return report.check("test_files", bool(reason),
                            f"none, because: {reason}" if reason else
                            "no test files and no no_new_tests_reason — write the tests first, "
                            "or say why this change has no new behaviour to test")
    changed = set(git_helper.changed_paths(run.repo_root))
    for test in tests:
        report.check(test, test in changed, "in the diff" if test in changed else
                     "not changed by this run — test_files lists only tests you added or changed")
    listed = ", ".join(f"`{t}`" for t in tests)
    if not report.passed:
        return report
    if not run.cfg.quality.test:
        run.report["tests"] = f"**Tests written first:** {listed} (not checked: no `quality.test`)"
        return report.check("red check", True, "skipped — quality.test is not configured")
    if run.worktree is None:
        run.report["tests"] = f"**Tests written first:** {listed} (not checked: in place)"
        return report.check("red check", True, "skipped — runs only in a worktree")
    implementation = sorted(changed - set(tests))
    if not implementation:
        return report.check("red check", False, "every changed file is declared a test file, "
                            "so there is no change for the tests to fail without")
    run.console.note(f"red check: {len(implementation)} implementation file(s) put back to HEAD; "
                     "the suite must FAIL")
    with git_helper.reverted(implementation, run.repo_root, run.session_dir / "red_check"):
        result = quality.test(run)
    red = not result.passed
    run.report["tests"] = (f"**Tests written first:** {listed}. With the change reverted the "
                           f"suite fails (exit {result.returncode}), so they test it." if red else
                           f"**Tests:** {listed}, which pass without the change.")
    return report.check(
        "red check", red,
        f"suite exited {result.returncode} without the {len(implementation)} implementation "
        "file(s): the new tests fail on the old code" if red else
        f"the suite PASSES with all {len(implementation)} implementation file(s) put back to "
        "HEAD, so the new tests do not test the change. Rewrite them to fail on the code "
        f"as it was, then pass on yours. Log: {result.output_artifact}")


def _repo_path(run, declared: str) -> str:
    """A path as git names it: repo-relative, forward slashes."""
    path = Path(declared)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(Path(run.repo_root).resolve())
        except ValueError:
            pass
    return path.as_posix()


def checklist_covered(prompt: str):
    """Gate factory: the review rules on every item of the prompt's Review checklist.

    An issue's acceptance criteria (a spec's user stories) arrive as that
    checklist (issues.as_prompt). A reviewer that summarizes them into its own
    requirements can drop one without anyone seeing; naming each is checkable.
    A prompt without the section has nothing to check.
    """
    checklist = issues.checklist_in(prompt)

    def gate(envelope: EnvelopeBase, run) -> GateReport:
        report = GateReport()
        findings = getattr(envelope, "findings", [])
        ruled = []
        for item in checklist:
            finding = next((f for f in findings if _key(item) in _key(f.requirement)), None)
            report.check(item, finding is not None,
                         ("met" if finding.met else "not met") + f" — {finding.evidence}"
                         if finding else "no finding rules on this item — add one whose "
                         "requirement is this text, verbatim")
            ruled.append(f"- [{'x' if finding and finding.met else ' '}] {item}"
                         + (f" — {finding.evidence}" if finding and finding.evidence else ""))
        if checklist:
            run.report["review"] = "**Review checklist:**\n" + "\n".join(ruled)
        return report
    gate.__name__ = "checklist_covered"
    return gate


def _key(text: str) -> str:
    return " ".join(re.sub(r"[*_`]", "", text).casefold().split()).strip(" .;:")


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                cwd=run.repo_root)   # the checkout the agent changed
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
