# Install

`/sssf install` — stamp the entire factory out of the skill and into the current working directory.

## Run it

```bash
uv run .claude/skills/sssf/scripts/install.py
```

Run from the **target repo root** — the cwd is where everything lands. If the skill lives in your user scope, the path is `~/.claude/skills/sssf/scripts/install.py`.

## What gets stamped

`install.py` copies `templates/` into the cwd:

| Stamped | From | Tracked? |
|---|---|---|
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` | yes — the agent roster |
| `.env.sample` | `templates/env.sample` | yes |
| `adws/adw_*.py` | `templates/adws/` | yes — the twelve starter ADWs |
| `adws/adw_modules/` | `templates/adws/adw_modules/` | yes — all low-level logic |
| `adws/adw_data/prompt_engineering/{planner,builder,scout,reviewer,documenter}/` | `templates/prompt_engineering/` | yes — **the user-owned home for prompts** |
| `adws/adw_data/harness_engineering/` | `templates/harness_engineering/` | yes — **the user-owned home for pi extensions** |
| `justfile` | `templates/justfile` | yes — starter recipes: `just demo`, the workflows, the trace reads, `just obs` |
| `adws/.sssf_stamp.json` | written by `install.py` | yes — a hash of every stamped file and where sssf came from, so an update can tell your edits from its own files and knows where to update from |
| `adws/adw_data/sessions/`, `adws/adw_data/sssf.db` | created at runtime | no — gitignored |

The two `*_engineering` dirs mirror the two config keys of the same name: `prompt_engineering` is what an agent is told, `harness_engineering` is what its harness can do. Both are yours the moment they are stamped. Edit them in `adws/adw_data/`, never back inside the skill.

`harness_engineering/` ships with `subagents.ts` — the pi extension backing `subagent_create` / `_continue` / `_list` / `_remove`, wired to the planner and scout in the starter roster.

## Updating an installed repo

```bash
just sssf-update                                  # from where this repo's sssf came from
just sssf-update --source <sssf repo path | git URL> [--ref <branch|tag|sha>]
```

Start from a clean tree, then review the result with `git diff` and commit it, `adws/.sssf_stamp.json` included. What happens to each stamped file:

| File | Unedited | Edited by you |
|---|---|---|
| sssf's code: `adws/adw_modules/`, the starter `adws/adw_*.py` | replaced | **stops the update before anything is written**, because modules half at one version and half at another break. Move your change into your own module or ADW, or pass `--overwrite-edited` (git keeps your version) |
| yours: the config, prompts, harness extensions, `justfile`, `.env.sample` | replaced | kept, and listed with the `git diff` to merge by hand if you want the new version |
| new in this version | added | |
| stamped once, since deleted by you | not restored, and listed | |

"Unedited" means a file matches what the stamp recorded, **or any version its template has had in sssf's git history**. So a repo installed before the stamp existed updates too, without `--force-all`.

Your config rarely needs merging. Defaults live in code, so a key the config doesn't set takes the new version's default, and the update lists keys that are new in this version. `protected_files` only adds to the built-in protections, so new ones reach your roster without an edit. A `justfile` you edited gets the `sssf-update` recipe appended if it lacks one. After updating, every ADW is loaded with `--help` (no agent runs), so a broken update shows up then, not partway through a run.

**Where it updates from.** Every install and update records its source in the stamp: the skill's path, and when it sits in a git repo, that repo's URL and commit. `just sssf-update` runs `adws/adw_modules/sssf_update.py`, which uses, in order:
1. `--source`
2. the recorded path, if it exists on this machine
3. the recorded git URL, cloned into `~/.cache/sssf/` (`SSSF_CACHE` overrides) and fetched on later runs

Whichever it finds, it runs that version's own `install.py --update`, so the update logic is always the newer one's.

**A copy of the skill inside the repo** (`.claude/skills/sssf`, as the README's quick start makes) can't update itself. It is only as new as the copy, and the update says so. Name the real source once with `--source`. From then on it is recorded, and each update also refreshes the in-repo copy, so `/sssf`, the cookbooks and `just obs` match the code. Build output and installed packages (`dist/`, `node_modules/`) are left alone.

**The first update** of a repo stamped before `just sssf-update` existed has no recipe yet. Run the newer installer directly, once, from the repo root:

```bash
uv run <path to the sssf repo>/.claude/skills/sssf/scripts/install.py --update
```

## Re-running the installer

Re-running `install.py` without a flag is safe. It skips **every** file that already exists and reports what it skipped, so a second run doubles as a drift check. `--force` refreshes only files still exactly as stamped, and `--force-all` overwrites everything, your edits included. Both predate `--update`, which is the way to take a newer version.

## Post-install checklist

1. **Env** — `cp .env.sample .env`, then set `OPENROUTER_API_KEY` in `.env`. Agents on `coding_agent: claude_code` use `claude`'s own auth instead — no key needed if you are logged in; set `CLAUDE_CODE_PATH` if `claude` is not on PATH.
2. **Pi is installed and on PATH** — `pi --version`. Set `PI_PATH` in `.env` if it is not.
3. **The model resolves** — the config's default `gemini-3.6-flash` must be a registered id in `~/.pi/agent/models.json`. Check with `pi --list-models` or read the file directly; see `references/config.md` for model resolution.
4. **Gitignore** — `install.py` appends `adws/adw_data/sessions/`, `adws/adw_data/sssf.db*`, and `.env` for you; confirm they landed. All three are runtime or secrets and must never be committed.
5. **Git repo** — ADWs that end in a commit phase run in their own git worktree of HEAD (`../<repo>.sssf-worktrees/<adw_id>`, branch `sssf/<adw_id>`), so they need a repo with at least one commit, and they build on your last commit, not on uncommitted edits. If your tests need installed dependencies or a `.env`, set `worktree: {copy, setup}` in the config. `--in-place` works in your checkout instead and refuses a dirty tree unless `--allow-dirty`. Run `git init` and make a first commit before using `adw_plan_build.py`, `adw_plan_build_test.py`, or `adw_simple_sdlc.py`. `adw_document.py` needs one too: it measures the change with `git diff` against a base ref (`main` by default, `--base` to override).
6. **Quality commands** — set `quality.test` in `sssf.config.yaml` to your real test argv. ADWs with a test phase refuse to start without it.
7. **Smoke test** — `just demo` runs two cheap read-only workflows back to back, or run the smallest ADW directly:

```bash
just demo                                                    # both, end to end
uv run adws/adw_prompt.py "reply with a one-line summary of this repo"   # the raw form
```

Green means the whole path works: config validated, session minted, Pi ran, envelope parsed, events landed in `adws/adw_data/sssf.db`. Verify the trace exists before trusting anything larger:

```bash
sqlite3 adws/adw_data/sssf.db "select adw_id, status from sessions order by started_at desc limit 1;"
```

If the smoke test fails, fix it before composing chains — every multi-agent ADW rides on this exact path.
