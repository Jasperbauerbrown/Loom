-- 0007: tool calls are searchable (2026-10-12). The transcript index held user,
-- assistant and tool_result events only, so transcript_search could never find a
-- command that had been run: a thread spent 47 searches looking for an orca-slicer
-- command line it had used before. The tool_call event's content (the JSON list of
-- {id, name, arguments}) is indexed as it is; transcript_search renders a hit as
-- TOOL CALL name(arguments), the transcript's own form.

DROP TRIGGER IF EXISTS events_fts_ai;
CREATE TRIGGER events_fts_ai AFTER INSERT ON events
    WHEN new.kind IN ('user', 'assistant', 'tool_call', 'tool_result')
BEGIN
    INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
END;

INSERT INTO events_fts(rowid, content)
    SELECT id, content FROM events WHERE kind = 'tool_call' ORDER BY id;
