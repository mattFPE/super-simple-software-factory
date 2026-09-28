#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the SSSF factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--force | --force-all]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(starter agents), adws/adw_data/harness_engineering/,
adws/adw_sssf_config/sssf.config.yaml, .env.sample, justfile, .gitignore entries.

Every stamped file's hash is recorded in adws/.sssf_stamp.json (commit it), so
the installer can tell a file you edited from one it wrote:

    (default)     existing files are skipped — a re-run is a drift check
    --force       refresh every file you have NOT modified since it was stamped;
                  files you edited are kept and listed, never overwritten
    --force-all   overwrite everything, your edits included (the old --force)
"""

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
MANIFEST = Path("adws") / ".sssf_stamp.json"

GITIGNORE_ENTRIES = [
    "adws/adw_data/sessions/",
    "adws/adw_data/sssf.db*",
    ".env",
    # The ADWs are Python, so importing adw_modules writes bytecode next to it.
    # Chains that end in a commit phase call `git add -A`, so without this a
    # stamped repo commits its own .pyc files — 15 of them showed up in the
    # first repo that was ever installed into from scratch.
    "__pycache__/",
    "*.pyc",
]


def digest(path: Path) -> str:
    """Content hash, line endings normalized: git's autocrlf rewrites every
    stamped file to CRLF on Windows, and that is not an edit."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


@dataclass
class Stamping:
    """One install pass: the mode, the manifest, and what happened to each file."""
    mode: str                                   # "skip" | "refresh" | "all"
    root: Path
    manifest: dict[str, str]
    stamped: list[str] = field(default_factory=list)      # new files
    refreshed: list[str] = field(default_factory=list)    # replaced, unmodified by you
    overwritten: list[str] = field(default_factory=list)  # replaced under --force-all
    kept: list[str] = field(default_factory=list)         # you edited it: left alone
    skipped: list[str] = field(default_factory=list)      # exists, default mode

    def write(self, src: Path, dest: Path, bucket: list[str]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        self.manifest[self.rel(dest)] = digest(dest)
        bucket.append(self.rel(dest))

    def rel(self, dest: Path) -> str:
        return dest.relative_to(self.root).as_posix()


def stamp(src: Path, dest: Path, run: Stamping) -> None:
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name != "__pycache__":
                stamp(child, dest / child.name, run)
        return
    if not dest.exists():
        run.write(src, dest, run.stamped)
        return
    rel = run.rel(dest)
    if run.mode == "skip":
        run.skipped.append(rel)
        return
    if run.mode == "all":
        run.write(src, dest, run.overwritten)
        return
    current = digest(dest)
    if current == digest(src):
        run.manifest[rel] = current             # already current; nothing to do
    elif run.manifest.get(rel) == current:
        run.write(src, dest, run.refreshed)     # still exactly what we stamped
    else:
        run.kept.append(rel)                    # edited since (or never recorded)


def ensure_gitignore(root: Path, stamped: list) -> None:
    gitignore = root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gitignore.open("a") as f:
            f.write("\n# sssf runtime\n" + "\n".join(missing) + "\n")
        stamped.append(f".gitignore (+{len(missing)} entries)")


def report(label: str, paths: list[str], hint: str = "") -> None:
    if paths:
        print(f"  {label}: {len(paths)}{hint}")
        for path in paths:
            print(f"    {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--force", action="store_true",
                       help="refresh files you have not modified since they were stamped")
    group.add_argument("--force-all", action="store_true",
                       help="overwrite every stamped file, your edits included")
    args = parser.parse_args()

    root = Path.cwd()
    manifest_path = root / MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    run = Stamping(mode="all" if args.force_all else "refresh" if args.force else "skip",
                   root=root, manifest=manifest)

    stamp(TEMPLATES / "adws", root / "adws", run)
    stamp(TEMPLATES / "prompt_engineering", root / "adws" / "adw_data" / "prompt_engineering", run)
    stamp(TEMPLATES / "harness_engineering", root / "adws" / "adw_data" / "harness_engineering", run)
    stamp(TEMPLATES / "sssf.config.yaml", root / "adws" / "adw_sssf_config" / "sssf.config.yaml", run)
    stamp(TEMPLATES / "env.sample", root / ".env.sample", run)
    # The recipes are part of the operating experience, and several cookbooks
    # plus the run banner tell you to use them, so a stamped repo has to have
    # them. Treated like any other stamped file.
    stamp(TEMPLATES / "justfile", root / "justfile", run)
    ensure_gitignore(root, run.stamped)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(dict(sorted(run.manifest.items())), indent=2) + "\n")

    print(f"sssf installed into {root}")
    report("stamped (new)", run.stamped)
    report("refreshed (unmodified since stamped)", run.refreshed)
    report("overwritten (--force-all)", run.overwritten)
    report("kept (you modified them)", run.kept,
           " — diff against the skill's templates/ to merge, or --force-all to overwrite")
    if run.skipped:
        print(f"  skipped (already exist): {len(run.skipped)} — "
              "--force refreshes the ones you have not modified")
    if not (run.stamped or run.refreshed or run.overwritten):
        print("  nothing to change")
    print("\nnext steps:")
    print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
    print("  2. just demo             # two cheap read-only runs, end to end")
    print("  3. just sessions         # what just happened")
    print("  4. just obs              # the trace UI in the background, needs bun")
    print("\n  no just? the raw form of step 2 is:")
    print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")
    return 0


if __name__ == "__main__":
    sys.exit(main())
