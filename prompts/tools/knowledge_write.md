Write to your vault. This is how you remember: durable facts, decisions, conventions,
how something is set up. Prefer one note per entity, small and true, over long logs.

Arguments:
- `path` (required): vault-relative, ending in `.md`. The folder is the entity type:
  `devices/`, `projects/`, `people/`, `connectors/`, `courses/`, `topics/`,
  `procedures/<name>/SKILL.md`, `log/YYYY-MM-DD.md`, `core/`. Writes under `core/` are
  shown to the user for approval, because those notes are in context every turn.
- `op` (required): how to write.
  - `create` — a new note. Fails if the path already exists.
  - `append` — add to the end, after a blank line. The cheapest correct write.
  - `replace` — swap `old` for `content`. `old` must appear exactly once; copy it from
    the note verbatim, with enough surrounding text to be unique.
  - `rethink` — replace the whole body. Requires `reason`, and is recorded on the user's
    now board. Use it when the note's shape is wrong, not to fix a line.
- `content` (required): the markdown body to write — no frontmatter block; that goes in
  the `frontmatter` argument (a leading `---` block here is folded into the frontmatter,
  never nested). Link other notes with `[[wikilinks]].`
  Dates must be absolute (`2026-09-20`), never "yesterday" or "next week" — a note
  outlives the conversation that wrote it.
- `old` (required for `replace`): the exact existing text to swap out.
- `frontmatter` (optional): a mapping merged into the note's frontmatter — `aliases`
  (the names the user says out loud: "the printer", "ender 3"), `tags`, `type`, `root`
  for projects. `title`, `type` and `modified` are filled in for you if you omit them.
- `reason` (required for `rethink`): one line on what was wrong with the old note.

Returns a confirmation naming the path, or an error explaining exactly what to fix.
