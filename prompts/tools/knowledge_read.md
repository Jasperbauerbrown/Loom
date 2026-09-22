Read a note from your vault in full, or a slice of its lines.

Arguments:
- `path` (required): the vault-relative path, exactly as search or a pre-loaded block
  showed it — for example `devices/ender-3.md`. Paths outside the vault are refused.
- `offset` (optional, default 0): the first line to return, counting from zero. Use it
  to page through a long note.
- `limit` (optional, default 400): how many lines to return.
- `pin` (optional): `true` keeps this note in your context every turn from now on, up
  to the configured cap per note, until you pass `pin: false` for it. Pin the notes you
  keep returning to — the project you are working in, the device you are operating —
  and unpin what you no longer need. A note that does not exist cannot be pinned.

Returns the note text with its frontmatter, and a footer naming the line range when the
note was longer than the slice, or saying that the note is now pinned or unpinned. To
find a note you cannot name, use `knowledge_search`.
