---
name: self-modification
description: Change your own code safely — isolated worktree, scratch state, tests, promotion with rollback.
type: procedure
---
# Self-modification

## When to use
When the work is a change to your own code or UI: a bug you were asked to diagnose, a
capability you were asked to give yourself, a fix you found while doing something else.
Not for reading your own code — that is ordinary file reading — and not for editing the
running tree, which you never do.

## Steps
1. **Spawn an isolated thread** for the change: `conversation_create` with the
   repository in `roots` and `isolation: "worktree"`. The thread gets its own checkout
   of the repository under `<APP_HOME>/worktrees/<thread-id>` on branch
   `loom/<thread-id>`; that checkout is its first root and its harness block names it.
   The live checkout is not among its roots, so nothing below can touch it and nothing
   below needs approving. Never make the worktree by hand with `git worktree add`: a
   checkout made that way is outside every root and every file call into it asks. One
   thread, one change; a thread that has promoted is finished.
2. **Make a scratch state directory** inside the thread's own scratch folder (the
   `scratch:` path in its harness block), e.g. `<scratch>/loom-home/`, and use it as
   `LOOM_HOME` for anything you run. It is already inside the roots. Live state is
   never a test fixture.
3. **Read before writing.** `docs/README.md`, then the section of
   `docs/03-technical-design.md` that owns what you are changing. The docs are
   authoritative; when the code and the docs disagree, the change includes the doc edit
   and a dated line in the charter's decision log.
4. **Change the code** in the worktree. Keep the hard rules in `CLAUDE.md`: no new
   dependencies, single entrypoints, persist before act, fail closed, model-facing prose
   in `prompts/`.
5. **Run the tests there**: `python -m uv run pytest` from the worktree. Add the test
   that would have caught the bug. If the change touches `ui/`, also
   `npm --prefix ui run build`, because the served app is the built bundle.
6. **Try it** if it is worth trying: `LOOM_HOME=<scratch>/loom-home python -m uv run
   loom serve --no-supervisor --port <a free port>` and exercise the change.
7. **Commit** in the worktree, on its `loom/<thread-id>` branch, with a message in the
   repository's style. The branch is how the work comes back.
8. **`promote_plan(<commit>)`** and tell the person what it says — especially any
   protected paths in the diff.
9. **`promote(<commit>)`** when they agree. That ends the turn: the supervisor
   snapshots, builds, probes, swaps and restarts, and rolls back anything that does not
   hold. You resume afterwards and report the outcome.

## Checks
- The worktree is clean and the tests pass at the commit you promote.
- `promote_plan` lists no protected path you have not explicitly raised.
- After the restart: the promote log's last entry says `done`, the System screen shows
  the new release, and the thing you set out to change actually behaves differently.
- If it says `failed`: you are running on the previous release, nothing is lost, and the
  message says which stage refused. Fix it in the same worktree and promote again.
