# Plan: Add PR smoke-test document

## Scope
Create only `docs/pr-smoke-test.md`.

## Implementation
1. Add `docs/pr-smoke-test.md` containing exactly one line:
   ```text
   SSSF --pr smoke test. Safe to close.
   ```
2. Do not modify any other tracked or untracked project files as part of this work.

## Verification
1. Read `docs/pr-smoke-test.md` and confirm its sole line matches `SSSF --pr smoke test. Safe to close.` exactly.
2. Run `git diff --check` to confirm no whitespace errors.
3. Inspect `git status --short` and confirm the only work change is the new `docs/pr-smoke-test.md` file.
