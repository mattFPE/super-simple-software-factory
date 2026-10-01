# A local Tracker is the engineer's checkout's state, and `resolved` travels with the code

In a Local Markdown repo the issues are committed files under `.scratch/`, so a worktree Run holds its own stale copy of every Ticket. We treat the engineer's checkout as the only live Tracker: claims, failures and outcome comments are written there, uncommitted, and the issue text reaches the agent through the prompt, never the worktree's copy. The one exception is success on a Run that merges: it commits the Ticket's `Status: resolved` and outcome comment on its own branch before merging, so "the code is on your branch" and "the Ticket says resolved" arrive in the same commit and a blocked Ticket can never unblock ahead of the code it needs. To keep that true, `.scratch/` is protected from agents and excluded from code commits, and clean-tree checks ignore it.

## Considered Options

- **Leave every Tracker edit uncommitted**: simplest, but `resolved` then depends on the engineer remembering to commit it, and dependents can unblock on code that was never merged.
- **Commit Tracker edits straight onto the engineer's branch**: two commits per Run on a branch the engineer is working on, made without asking.
