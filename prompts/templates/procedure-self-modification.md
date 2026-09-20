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
1. **Spawn a thread** for the change with the repository as its scope. One thread, one
   change; a thread that has promoted is finished.
2. **Make a worktree.** `git worktree add <APP_HOME>/releases/dev-<thread-id> -b
   fix/<short-name>` in the repository root. Everything below happens in that directory;
   the live checkout is never edited.
3. **Make a scratch state directory** at `<APP_HOME>/scratch/<thread-id>/` and use it as
   `LOOM_HOME` for anything you run. Live state is never a test fixture.
4. **Read before writing.** `docs/README.md`, then the section of
   `docs/03-technical-design.md` that owns what you are changing. The docs are
   authoritative; when the code and the docs disagree, the change includes the doc edit
   and a dated line in the charter's decision log.
5. **Change the code** in the worktree. Keep the hard rules in `CLAUDE.md`: no new
   dependencies, single entrypoints, persist before act, fail closed, model-facing prose
   in `prompts/`.
6. **Run the tests there**: `python -m uv run pytest` from the worktree. Add the test
   that would have caught the bug. If the change touches `ui/`, also
   `npm --prefix ui run build`, because the served app is the built bundle.
7. **Try it** if it is worth trying: `LOOM_HOME=<scratch> python -m uv run loom serve
   --no-supervisor --port <a free port>` and exercise the change.
8. **Commit** in the worktree, with a message in the repository's style.
9. **`promote_plan(<commit>)`** and tell the person what it says — especially any
   protected paths in the diff.
10. **`promote(<commit>)`** when they agree. That ends the turn: the supervisor
    snapshots, builds, probes, swaps and restarts, and rolls back anything that does not
    hold. You resume afterwards and report the outcome.

## Checks
- The worktree is clean and the tests pass at the commit you promote.
- `promote_plan` lists no protected path you have not explicitly raised.
- After the restart: the promote log's last entry says `done`, the System screen shows
  the new release, and the thing you set out to change actually behaves differently.
- If it says `failed`: you are running on the previous release, nothing is lost, and the
  message says which stage refused. Fix it in the same worktree and promote again.
