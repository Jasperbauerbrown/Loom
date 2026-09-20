Bring a commit of your own code live. Restart is the deploy: this ends the turn.

Call `promote_plan(ref)` first and tell the person what it says. Then `promote(ref)`:

1. the ref must resolve to a commit, and the worktree it lives in must be clean;
2. a diff touching protected paths refuses unless you pass
   `acknowledge_protected: true` — do that only after you have named those files to the
   person and they said to go ahead; the call still needs their approval;
3. the full test suite runs in that commit's worktree, and a single failure stops the
   promotion with the output;
4. the supervisor then snapshots the database, builds the release, probes it on a
   scratch copy of the state, swaps and restarts, and watches health. Anything that
   does not hold is rolled back automatically.

Once this returns, the turn is over: you are the process being replaced. Say so and
stop. When you come back, the outcome is waiting for you as a system event and an item
on the now board; report it then.

Promote your own commits only — work in the thread's worktree, run the tests there,
commit, and promote that commit. Never promote code you have not read.
