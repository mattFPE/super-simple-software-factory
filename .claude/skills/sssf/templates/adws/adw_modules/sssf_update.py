#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""just sssf-update — bring this repo's sssf up to date from where it came from.

    uv run adws/adw_modules/sssf_update.py [--source PATH|GIT-URL] [--ref REF]
                                           [--overwrite-edited] [--allow-dirty]

This file only FINDS the newer sssf; the update itself is that version's own
`install.py --update`, so the logic that decides what to replace is always the
new one, never whatever this repo was stamped with.

Where from, first match wins:
    --source             a path (the sssf repo, or its skill dir) or a git URL
    the stamp's path     adws/.sssf_stamp.json, if it still exists on this machine
    the stamp's git URL  cloned into ~/.cache/sssf/ (SSSF_CACHE), fetched on
                         later runs, checked out at --ref or the default branch

Standalone and stdlib-only on purpose: it has to run when the rest of
adw_modules is out of date, or broken.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # adws/adw_modules/ -> the repo
STAMP = ROOT / "adws" / ".sssf_stamp.json"
SKILL_DIR = ".claude/skills/sssf"                   # where the skill sits in the sssf repo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="sssf to update from: a path or a git URL")
    parser.add_argument("--ref", help="with a git source: the branch, tag or commit to take")
    parser.add_argument("--overwrite-edited", action="store_true",
                        help="replace sssf code you edited instead of stopping")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="run on uncommitted changes, or outside git")
    args = parser.parse_args()

    source = _stamp_source()
    try:
        installer = _installer(args.source, args.ref, source)
    except RuntimeError as error:
        print(f"sssf-update: {error}", file=sys.stderr)
        return 1
    passthrough = [f for f, on in (("--overwrite-edited", args.overwrite_edited),
                                   ("--allow-dirty", args.allow_dirty)) if on]
    print(f"updating from {installer.parent.parent}", flush=True)
    return subprocess.run([sys.executable, str(installer), "--update", *passthrough],
                          cwd=ROOT).returncode


def _stamp_source() -> dict:
    try:
        data = json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("source") or {}


def _installer(given: str | None, ref: str | None, source: dict) -> Path:
    skill_dir = source.get("skill_dir") or SKILL_DIR
    if given:
        if _is_url(given):
            return _from_git(given, ref, skill_dir)
        found = _in_path(Path(given), skill_dir)
        if not found:
            raise RuntimeError(f"no sssf installer under {given} "
                               f"(looked for scripts/install.py and {skill_dir}/scripts/install.py)")
        return _no_ref(found, ref)
    if source.get("path") and (found := _in_path(Path(source["path"]), skill_dir)):
        return _no_ref(found, ref)
    if source.get("git"):
        return _from_git(source["git"], ref, skill_dir)
    raise RuntimeError(
        "don't know where this repo's sssf came from: adws/.sssf_stamp.json names no source "
        "that exists here. Pass --source <path to the sssf repo, or its git URL>. Every "
        "update records the source, so this is needed once.")


def _in_path(path: Path, skill_dir: str) -> Path | None:
    for candidate in (path / "scripts" / "install.py", path / skill_dir / "scripts" / "install.py"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _no_ref(installer: Path, ref: str | None) -> Path:
    if ref:
        raise RuntimeError("--ref needs a git URL source; a local path is used as it is on disk")
    return installer


def _is_url(text: str) -> bool:
    return bool(re.match(r"^(\w+://|[\w.-]+@[\w.-]+:)", text))


def _from_git(url: str, ref: str | None, skill_dir: str) -> Path:
    """A clone of `url` in the cache, fetched and checked out at `ref`."""
    cache = Path(os.environ.get("SSSF_CACHE") or Path.home() / ".cache" / "sssf")
    # The repo's name plus a hash of the URL: unique, and short, because git
    # for Windows refuses a clone whose paths outgrow MAX_PATH.
    name = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "sssf"
    safe = re.sub(r"[^\w.-]+", "_", name)
    clone = cache / f"{safe}-{hashlib.sha1(url.encode()).hexdigest()[:8]}"
    if (clone / ".git").is_dir():
        _run(["git", "-C", str(clone), "fetch", "--quiet", "--tags", "origin"])
    else:
        clone.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--quiet", url, str(clone)])   # full history: see template_history
    target = f"origin/{ref}" if ref and _ok(clone, f"origin/{ref}") else ref or "origin/HEAD"
    _run(["git", "-C", str(clone), "checkout", "--quiet", "--detach", target])
    found = _in_path(clone, skill_dir)
    if not found:
        raise RuntimeError(f"{url} has no {skill_dir}/scripts/install.py at {target}")
    return found


def _ok(clone: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(clone), "rev-parse", "--verify", "--quiet", ref],
                          capture_output=True).returncode == 0


def _run(argv: list[str]) -> None:
    done = subprocess.run(argv, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {done.stderr.strip()}")


if __name__ == "__main__":
    sys.exit(main())
