Read a note from your vault in full, or a slice of its lines.

Arguments:
- `path` (required): the vault-relative path, exactly as search or a pre-loaded block
  showed it — for example `devices/ender-3.md`. Paths outside the vault are refused.
- `offset` (optional, default 0): the first line to return, counting from zero. Use it
  to page through a long note.
- `limit` (optional, default 400): how many lines to return.

Returns the note text with its frontmatter, and a footer naming the line range when the
note was longer than the slice. To find a note you cannot name, use `knowledge_search`.
