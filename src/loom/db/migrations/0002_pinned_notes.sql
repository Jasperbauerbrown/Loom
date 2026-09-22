-- 0002: threads and projects separated (03 §6.7, decided 2026-09-22). A thread no longer
-- scopes to one project note: it pins any vault notes it needs (`pinned_notes`, a json
-- list of vault-relative paths) and keeps its status in its own transcript. Projects are
-- ordinary knowledge the model writes. `walkthrough` records where finish filed the
-- thread's walkthrough (a section of the daily log). The projects-root setting is gone
-- with the auto-created project note it fed.

ALTER TABLE conversations ADD COLUMN pinned_notes TEXT NOT NULL DEFAULT '[]';
ALTER TABLE conversations ADD COLUMN walkthrough TEXT;
UPDATE conversations SET pinned_notes = json_array(scope_note) WHERE scope_note IS NOT NULL AND scope_note != '';
ALTER TABLE conversations DROP COLUMN scope_note;
DELETE FROM settings WHERE key = 'general.projects_root';
