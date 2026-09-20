Show what promoting a commit of your own code would change, without changing anything.

Give a ref: a commit sha, a tag, or a branch name in your own repository. You get back
the commit it resolves to, the commit currently live, how many files differ, whether the
worktree that holds the ref has uncommitted changes, and — the part that matters — which
of the changed files are **protected paths** (the supervisor, the permission and
promotion code, the secrets store).

Always call this before `promote`. Read the result out to the person: they decide on a
promotion with the protected-path list in front of them, and a promotion that touches
one refuses until they have seen it.
