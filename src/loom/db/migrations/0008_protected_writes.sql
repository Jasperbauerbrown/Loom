-- 0008: protected paths gain a mode (2026-10-12). `all` is the old meaning: any call
-- that touches the path asks. `writes` asks only for a call that writes, edits, moves
-- or deletes there; reading and running stay free. It exists for the vault's procedure
-- scripts (`procedures/*/scripts`), which threads run by name. Existing rows keep `all`.
-- The default `writes` row is seeded by bootstrap (it needs the vault path), once: on
-- a fresh install, or on the start that applies this migration.

ALTER TABLE protected_paths ADD COLUMN mode TEXT NOT NULL DEFAULT 'all' CHECK (mode IN ('all', 'writes'));
