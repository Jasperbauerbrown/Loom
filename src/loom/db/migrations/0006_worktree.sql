-- 0006: worktree isolation for any thread (05-backlog, audit #15), generalising the
-- self-modification worktree of 03 §15. A thread created with isolation="worktree"
-- records the checkout it works in so it can be listed, offered as a root and removed.

ALTER TABLE conversations ADD COLUMN worktree TEXT;   -- absolute path, NULL when none
