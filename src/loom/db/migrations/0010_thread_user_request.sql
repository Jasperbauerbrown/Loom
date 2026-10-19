-- 0010: a thread keeps the user's own words it was started from (2026-10-19). The
-- permission judge sees the user's typed desk request on the desk; a thread's turns are
-- claimed from the desk model's goal and handoff, which authorise nothing, so the judge in
-- a thread had no request at all. The spawning desk turn's typed message (or the goal of a
-- thread the user created themselves) is stored here and inherited by sub-threads.

ALTER TABLE conversations ADD COLUMN user_request TEXT;
