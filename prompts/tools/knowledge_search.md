Search your vault — the markdown notes that are your memory of entities, projects,
people, devices, courses and topics. Words and meaning are searched together (full-text
rank fused with embedding similarity), so a paraphrase finds the note too.

Use this when you need something you were not handed. Notes relevant to the user's
message are often pre-loaded for you already; search when they are not, when you need a
second entity, or when you want to check whether something is written down before
writing it again.

Arguments:
- `query` (required): what you are looking for, in ordinary words. Names, aliases and
  descriptions all match; there is no query syntax to learn.
- `type` (optional): restrict to one note type — `device`, `project`, `person`,
  `connector`, `course`, `topic`, `procedure`, `core`, `log`. Omit to search everything.
- `k` (optional, default 8): how many results to return, 1–50.

Returns one line per hit: path, title, type and a matching snippet. Read the ones that
look right with `knowledge_read`; the snippet is an excerpt, not the note.
