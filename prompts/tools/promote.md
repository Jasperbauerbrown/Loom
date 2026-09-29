Bring a commit of your own code live. Restart is the deploy: this ends the turn.

Call `promote_plan(ref)` first and tell the person what it says. Then `promote(ref)`:

1. the ref must resolve to a commit, and the worktree it lives in must be clean;
2. a diff touching protected paths refuses unless you pass
   `acknowledge_protected: true` — do that only after you have named those files to the
   person and they said to go ahead; the call still needs their approval;
3. the call returns as soon as the supervisor has the ref. The supervisor then snapshots
   the database, builds the release, runs the full test suite inside it, probes it on a
   scratch copy of the state, swaps and restarts, and watches health. Every stage lands
   in the promote log and on the System page; a failing suite, or anything else that
   does not hold, is rolled back automatically with the reason.

Once this returns, the turn is over: you are the process being replaced. Say so and
stop. The outcome arrives as a message in this conversation when it is known, minutes
later; report it then. You need not run the full suite before promoting — the
supervisor runs it in the exact release that will go live, and that run is the one that
counts.

Promote your own commits only — work in the thread's worktree, commit, and promote that
commit. Never promote code you have not read.
