#!/usr/bin/env -S uv run
# /// script
# dependencies = []
# ///
"""/install — stamp the SSSF factory from the skill into the cwd. Idempotent.

Usage:
    uv run <skill>/scripts/install.py [--update [--overwrite-edited] [--allow-dirty]
                                       | --force | --force-all]

Stamps: adws/ (modules + starter ADWs), adws/adw_data/prompt_engineering/
(starter agents), adws/adw_data/harness_engineering/,
adws/adw_sssf_config/sssf.config.yaml, .env.sample, justfile, .gitignore entries.

adws/.sssf_stamp.json (commit it) records the hash of every stamped file and
where sssf came from, so the installer can tell a file you edited from one it
wrote, and `just sssf-update` knows where to update from:

    (default)     existing files are skipped — a re-run is a drift check
    --update      bring an installed repo up to this version; what
                  `just sssf-update` runs (see update())
    --force       refresh every file you have NOT modified since it was stamped;
                  files you edited are kept and listed, never overwritten
    --force-all   overwrite everything, your edits included
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL / "templates"
MANIFEST = Path("adws") / ".sssf_stamp.json"
UPDATE_SCRIPT = "adws/adw_modules/sssf_update.py"
# Where the README's quick start copies the skill into a repo. A copy there is
# what `/sssf` and `just obs` use, so an update from a newer sssf refreshes it.
SKILL_COPY = Path(".claude") / "skills" / "sssf"
SKILL_COPY_SKIP = {"node_modules", "dist", "__pycache__", ".git"}
SMOKE_TIMEOUT = 180

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

# The recipe `--update` adds to a justfile you edited, when it has none.
UPDATE_RECIPE = """
# update sssf's code from where it was installed: just sssf-update [--source <path|git url>]
sssf-update *ARGS:
    uv run adws/adw_modules/sssf_update.py "$@"
"""


def digest(path: Path) -> str:
    """Content hash, line endings normalized: git's autocrlf rewrites every
    stamped file to CRLF on Windows, and that is not an edit."""
    return _digest(path.read_bytes())


def _digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _git(cwd: Path, *args: str) -> str | None:
    """stdout of a git command, or None when it fails (or git is missing)."""
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# ── what gets stamped where ──────────────────────────────────────────────────

@dataclass
class Target:
    src: Path          # the template file
    dest: Path         # where it lands in the repo
    rel: str           # dest, repo-relative
    template: str      # src, relative to templates/ — the key into its git history
    sssf_code: bool    # adws/: sssf's machinery, not yours to keep diverged


def targets(root: Path) -> list[Target]:
    pairs = [
        (TEMPLATES / "adws", root / "adws"),
        (TEMPLATES / "prompt_engineering", root / "adws" / "adw_data" / "prompt_engineering"),
        (TEMPLATES / "harness_engineering", root / "adws" / "adw_data" / "harness_engineering"),
        (TEMPLATES / "sssf.config.yaml", root / "adws" / "adw_sssf_config" / "sssf.config.yaml"),
        (TEMPLATES / "env.sample", root / ".env.sample"),
        # The recipes are part of the operating experience, and several cookbooks
        # plus the run banner tell you to use them, so a stamped repo has to have
        # them. Treated like any other stamped file.
        (TEMPLATES / "justfile", root / "justfile"),
    ]
    found = []
    for src_root, dest_root in pairs:
        files = ([src_root] if src_root.is_file() else
                 sorted(p for p in src_root.rglob("*")
                        if p.is_file() and "__pycache__" not in p.parts))
        for src in files:
            dest = dest_root if src_root.is_file() else dest_root / src.relative_to(src_root)
            template = src.relative_to(TEMPLATES).as_posix()
            found.append(Target(src, dest, dest.relative_to(root).as_posix(), template,
                                sssf_code=template.startswith("adws/")))
    return found


# ── the stamp file ───────────────────────────────────────────────────────────

def read_manifest(root: Path) -> dict[str, str]:
    """rel -> digest. Before 2026-09-29 the whole file was that mapping."""
    path = root / MANIFEST
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return data.get("files", {}) if isinstance(data.get("files"), dict) else data


def read_source(root: Path) -> dict:
    """The source the stamp recorded; {} for a stamp from before sources were."""
    path = root / MANIFEST
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return (data.get("source") or {}) if isinstance(data.get("files"), dict) else {}


def write_manifest(root: Path, files: dict[str, str]) -> None:
    path = root / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"source": source_info(root), "files": dict(sorted(files.items()))}
    text = json.dumps(data, indent=2) + "\n"
    # Unchanged means untouched: a rewrite on Windows would leave CRLF where a
    # repo keeps LF, and git would list the stamp as modified after a no-op.
    if path.is_file() and path.read_text(encoding="utf-8").replace("\r\n", "\n") == text:
        return
    path.write_text(text, encoding="utf-8", newline="\n")


def source_info(root: Path) -> dict:
    """Where this sssf came from: what sssf_update.py looks for next time.

    A skill inside the repo is a copy (the README's quick start makes one).
    Its git is the repo's own, so no URL or commit is recorded for it: the
    update says to name the real source once, with --source.
    """
    info: dict = {"path": SKILL.as_posix()}
    if SKILL.is_relative_to(root.resolve()):
        info["copy_in_repo"] = True
        return info
    top = _git(SKILL, "rev-parse", "--show-toplevel")
    if top:
        info["skill_dir"] = SKILL.relative_to(Path(top).resolve()).as_posix()
        info["commit"] = _git(SKILL, "rev-parse", "HEAD")
        if url := _git(SKILL, "remote", "get-url", "origin"):
            info["git"] = url
        info["uncommitted"] = bool(_git(SKILL, "status", "--porcelain", "--", "."))
    return info


def template_history() -> dict[str, set[str]]:
    """template path -> the digest of every version it has had in sssf's git history.

    What makes an update safe without a stamp file: a file that matches ANY
    version sssf ever shipped is sssf's, not an edit of yours, whether or not
    the repo recorded which version it got.
    """
    top = _git(TEMPLATES, "rev-parse", "--show-toplevel")
    if not top:
        return {}
    prefix = TEMPLATES.relative_to(Path(top).resolve()).as_posix() + "/"
    log = _git(Path(top), "log", "--no-renames", "--format=", "--raw", "--no-abbrev",
               "--", prefix) or ""
    blobs: dict[str, set[str]] = {}
    for line in log.splitlines():
        if not line.startswith(":") or "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        if not path.startswith(prefix):
            continue
        for blob in meta.split()[2:4]:
            if set(blob) != {"0"}:
                blobs.setdefault(path[len(prefix):], set()).add(blob)
    wanted = sorted({b for group in blobs.values() for b in group})
    if not wanted:
        return {}
    done = subprocess.run(["git", "cat-file", "--batch"], cwd=top, capture_output=True,
                          input=("\n".join(wanted) + "\n").encode())
    contents: dict[str, str] = {}
    out, pos = done.stdout, 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        pos = end + 1
        if len(header) < 3:                   # "<sha> missing"
            continue
        size = int(header[2])
        contents[header[0].decode()] = _digest(out[pos:pos + size])
        pos += size + 1
    return {path: {contents[b] for b in group if b in contents} for path, group in blobs.items()}


# ── install, --force, --force-all ────────────────────────────────────────────

@dataclass
class Stamping:
    """One install pass: the mode, the manifest, and what happened to each file."""
    mode: str                                   # "skip" | "refresh" | "all"
    manifest: dict[str, str]
    stamped: list[str] = field(default_factory=list)      # new files
    refreshed: list[str] = field(default_factory=list)    # replaced, unmodified by you
    overwritten: list[str] = field(default_factory=list)  # replaced under --force-all
    kept: list[str] = field(default_factory=list)         # you edited it: left alone
    skipped: list[str] = field(default_factory=list)      # exists, default mode

    def write(self, target: Target, bucket: list[str]) -> None:
        copy(target)
        self.manifest[target.rel] = digest(target.dest)
        bucket.append(target.rel)


def copy(target: Target) -> None:
    target.dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target.src, target.dest)


def stamp(target: Target, run: Stamping) -> None:
    if not target.dest.exists():
        run.write(target, run.stamped)
        return
    if run.mode == "skip":
        run.skipped.append(target.rel)
        return
    if run.mode == "all":
        run.write(target, run.overwritten)
        return
    current = digest(target.dest)
    if current == digest(target.src):
        run.manifest[target.rel] = current      # already current; nothing to do
    elif run.manifest.get(target.rel) == current:
        run.write(target, run.refreshed)        # still exactly what we stamped
    else:
        run.kept.append(target.rel)             # edited since (or never recorded)


def install(root: Path, mode: str) -> int:
    run = Stamping(mode=mode, manifest=read_manifest(root))
    for target in targets(root):
        stamp(target, run)
    ensure_gitignore(root, run.stamped)
    write_manifest(root, run.manifest)

    print(f"sssf installed into {root}")
    report("stamped (new)", run.stamped)
    report("refreshed (unmodified since stamped)", run.refreshed)
    report("overwritten (--force-all)", run.overwritten)
    report("kept (you modified them)", run.kept,
           ": diff against the skill's templates/ to merge, or --force-all to overwrite")
    if run.skipped:
        print(f"  skipped (already exist): {len(run.skipped)}: "
              "`--update` brings them up to this version")
    if not (run.stamped or run.refreshed or run.overwritten):
        print("  nothing to change")
    print("\nnext steps:")
    print("  1. cp .env.sample .env   # then set the key(s) your roster needs")
    print("  2. just demo             # two cheap read-only runs, end to end")
    print("  3. just sessions         # what just happened")
    print("  4. just obs              # the trace UI in the background, needs bun")
    print("\n  no just? the raw form of step 2 is:")
    print("     uv run adws/adw_prompt.py \"say hello\" --agent scout")
    print("\n  later, to take a newer sssf: just sssf-update")
    return 0


# ── --update ─────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    added: list[Target] = field(default_factory=list)       # new in this version
    updated: list[Target] = field(default_factory=list)     # sssf's, unedited: replaced
    unchanged: list[Target] = field(default_factory=list)   # already this version
    kept: list[Target] = field(default_factory=list)        # yours, edited: left alone
    conflicts: list[Target] = field(default_factory=list)   # sssf's code, edited: blocks
    removed: list[Target] = field(default_factory=list)     # stamped once, deleted by you


def plan_update(root: Path, manifest: dict[str, str], history: dict[str, set[str]]) -> Plan:
    plan = Plan()
    for target in targets(root):
        if not target.dest.exists():
            (plan.removed if target.rel in manifest else plan.added).append(target)
            continue
        current = digest(target.dest)
        if current == digest(target.src):
            plan.unchanged.append(target)
        elif current == manifest.get(target.rel) or current in history.get(target.template, ()):
            plan.updated.append(target)
        elif target.sssf_code:
            plan.conflicts.append(target)
        else:
            plan.kept.append(target)
    return plan


def update(root: Path, overwrite_edited: bool, allow_dirty: bool) -> int:
    """Bring an installed repo up to this version of sssf.

    Every stamped file lands in one of three places:

      sssf's code (adws/)   replaced when it is unedited; an edit STOPS the
                            update before anything is written, because modules
                            half at one version and half at another break.
                            --overwrite-edited replaces them (git keeps yours).
      yours (config, prompts, harness, justfile, .env.sample)
                            replaced only when unedited; an edit is kept and
                            listed. The config holds only your changes to the
                            defaults, which live in code, so it rarely needs any.
      new in this version   added.

    Unedited means the file matches what the stamp recorded, or any version of
    its template in sssf's git history, so repos installed before the stamp
    existed update too. Nothing runs against a dirty tree: the update is meant
    to be reviewed with `git diff` and undone with git.
    """
    if not (root / "adws" / "adw_modules").is_dir():
        print("sssf is not installed here: run install.py without --update")
        return 1
    dirty = _git(root, "status", "--porcelain", "--", "adws", "justfile", ".env.sample",
                 ".gitignore", *([SKILL_COPY.as_posix()] if (root / SKILL_COPY).is_dir() else []))
    if dirty is None and not allow_dirty:
        print("not a git repository: an update needs git, so every change it makes can be "
              "reviewed and undone. --allow-dirty runs it anyway.")
        return 1
    if dirty and not allow_dirty:
        print("uncommitted changes where sssf lives: commit or stash them first, so "
              "`git diff` shows only what the update did:")
        print("\n".join(f"  {line}" for line in dirty.splitlines()[:20]))
        return 1

    manifest = read_manifest(root)
    history = template_history()
    plan = plan_update(root, manifest, history)
    if plan.conflicts and not overwrite_edited:
        print("update stopped, nothing written: you edited sssf's own code, and this version "
              "changes it:")
        for target in plan.conflicts:
            print(f"  {target.rel}")
            print(f"      git diff --no-index {target.dest.as_posix()} {target.src.as_posix()}")
        print("Move what you changed into your own module or ADW, then update, or rerun with "
              "--overwrite-edited to replace them (git keeps your versions).")
        return 1

    for target in [*plan.added, *plan.updated, *plan.conflicts]:
        copy(target)
        manifest[target.rel] = digest(target.dest)
    for target in plan.unchanged:
        manifest[target.rel] = digest(target.dest)
    notes = []
    justfile = root / "justfile"
    if justfile.is_file() and not re.search(r"(?m)^sssf-update\b", justfile.read_text(encoding="utf-8")):
        with justfile.open("a", encoding="utf-8") as f:
            f.write(UPDATE_RECIPE)
        notes.append("justfile: added the sssf-update recipe to your edited justfile")
    gitignore: list[str] = []
    ensure_gitignore(root, gitignore)
    notes += gitignore
    notes += refresh_skill_copy(root)
    # A no-op leaves the stamp alone, unless it has no source yet or the source
    # moved (a first --source): rewriting it only to note, say, that the source
    # checkout has uncommitted edits would make every no-op update a diff.
    old, new = read_source(root), source_info(root)
    moved = (old.get("path"), old.get("git")) != (new.get("path"), new.get("git"))
    if plan.added or plan.updated or plan.conflicts or notes or moved:
        write_manifest(root, manifest)

    print(f"sssf updated in {root} from {SKILL}")
    report("added (new in this version)", [t.rel for t in plan.added])
    report("updated", [t.rel for t in plan.updated])
    report("overwritten (--overwrite-edited)", [t.rel for t in plan.conflicts])
    report("kept (you edited them; this version changes them)", [t.rel for t in plan.kept],
           ": merge by hand if you want the new version:")
    for target in plan.kept:
        print(f"      git diff --no-index {target.dest.as_posix()} {target.src.as_posix()}")
    config = [t for t in plan.kept if t.template == "sssf.config.yaml"]
    if config:
        new_keys = config_keys(config[0].src) - config_keys(config[0].dest)
        if new_keys:
            print(f"  config: new in this version, using its defaults until you set them: "
                  f"{', '.join(sorted(new_keys))}")
    report("not restored (stamped once, since deleted by you)", [t.rel for t in plan.removed])
    for note in notes:
        print(f"  {note}")
    if not (plan.added or plan.updated or plan.conflicts or notes):
        print("  already up to date")
    if SKILL.is_relative_to(root.resolve()):
        print(f"  note: this updated from the repo's own copy of the skill ({SKILL_COPY.as_posix()}),\n"
              "        which is only as new as that copy. To take a newer sssf, name it once:\n"
              "        just sssf-update --source <path to the sssf repo, or its git URL>")
    smoke(root)
    print("\nnext: git diff   # review it, then commit, adws/.sssf_stamp.json included")
    return 0


def refresh_skill_copy(root: Path) -> list[str]:
    """Mirror this skill into the repo's copy of it, when it has one and this is not it.

    The copy is sssf's, like adws/adw_modules/: its cookbooks and templates must
    match the code just stamped. Build output and installed packages are left alone.
    """
    copy_dir = root / SKILL_COPY
    if not copy_dir.is_dir() or copy_dir.resolve() == SKILL:
        return []
    def files(base: Path) -> dict[str, Path]:
        return {p.relative_to(base).as_posix(): p for p in base.rglob("*")
                if p.is_file() and not SKILL_COPY_SKIP & set(p.relative_to(base).parts)}
    ours, theirs = files(SKILL), files(copy_dir)
    changed = 0
    for rel, src in ours.items():
        dest = copy_dir / rel
        if rel not in theirs or digest(dest) != digest(src):
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            changed += 1
    for rel in theirs.keys() - ours.keys():
        theirs[rel].unlink()
        changed += 1
    return [f"skill copy: {SKILL_COPY.as_posix()} refreshed ({changed} file(s))"] if changed else []


def config_keys(path: Path) -> set[str]:
    """Top-level and `defaults.` keys of a config. Line-based: no yaml here."""
    keys, section = set(), None
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"^([A-Za-z_]+):", line):
            section = match.group(1)
            keys.add(section)
        elif section == "defaults" and (match := re.match(r"^  ([A-Za-z_]+):", line)):
            keys.add(f"defaults.{match.group(1)}")
    return keys


def smoke(root: Path) -> None:
    """Import every ADW (`--help` runs no agent) so a broken update fails here, not mid-run."""
    uv = shutil.which("uv")
    scripts = sorted((root / "adws").glob("adw_*.py"))
    if not uv or not scripts:
        return
    print(f"  smoke: loading {len(scripts)} ADW(s)...", flush=True)
    broken = []
    for script in scripts:
        try:
            done = subprocess.run([uv, "run", "-q", str(script.relative_to(root)), "--help"],
                                  cwd=root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=SMOKE_TIMEOUT)
            if done.returncode != 0:
                broken.append((script.name, (done.stderr or done.stdout).strip().splitlines()[-1:]))
        except subprocess.TimeoutExpired:
            broken.append((script.name, ["timed out"]))
    if broken:
        print(f"  smoke: {len(broken)} of {len(scripts)} ADW(s) failed to load:")
        for name, tail in broken:
            print(f"    {name}: {' '.join(tail)}")
    else:
        print(f"  smoke: all {len(scripts)} ADW(s) load")


# ── shared ───────────────────────────────────────────────────────────────────

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
    group.add_argument("--update", action="store_true",
                       help="bring an installed repo up to this version (just sssf-update)")
    group.add_argument("--force", action="store_true",
                       help="refresh files you have not modified since they were stamped")
    group.add_argument("--force-all", action="store_true",
                       help="overwrite every stamped file, your edits included")
    parser.add_argument("--overwrite-edited", action="store_true",
                        help="with --update: replace sssf code you edited instead of stopping")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="with --update: run on uncommitted changes, or outside git")
    args = parser.parse_args()
    if (args.overwrite_edited or args.allow_dirty) and not args.update:
        parser.error("--overwrite-edited and --allow-dirty go with --update")

    root = Path.cwd()
    if args.update:
        return update(root, args.overwrite_edited, args.allow_dirty)
    return install(root, "all" if args.force_all else "refresh" if args.force else "skip")


if __name__ == "__main__":
    sys.exit(main())
