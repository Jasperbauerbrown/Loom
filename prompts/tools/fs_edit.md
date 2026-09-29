Replace an exact piece of text in a file.

Arguments:
- `path` (string, required): the file to edit. Must be inside an allowed root.
- `old` (string, required unless `edits` is given): the exact text to find, including indentation and line breaks. It must occur exactly once unless `replace_all` is true; if it occurs more often, include more surrounding context.
- `new` (string, required unless `edits` is given): the text to put in its place. An empty string deletes the matched text.
- `replace_all` (boolean, optional, default false): replace every occurrence instead of requiring a unique one.
- `edits` (array of `{old, new, replace_all}`, optional): several edits, applied in order to one file, all or none — if any one fails to match, nothing is written and the file is untouched. Use this instead of `old`/`new`, never both in the same call; the result is a single diff of the whole change.

Returns the number of replacements and a short diff of what changed. Read the file first (`fs_read`): an edit whose `old` does not match fails and changes nothing, and the error shows the closest text in the file as a diff so you can correct `old` against it rather than guess again. Line endings and trailing whitespace are not compared, since you cannot see them; everything else — indentation, wording, where the lines break — must match exactly. An edit after someone else has changed the file on disk since your last read is refused — read it again and redo the change against what is there now. `old`/`new` must not contain `fs_read`'s `N\t` line-number prefixes; that numbering is not part of the file's text. The file's line endings and any BOM are preserved. Against a `.ipynb` notebook, `old`/`new` match the cell sources; outputs, metadata, execution counts and indentation are left as they were. A relative path that names an existing vault note is refused with a pointer to `knowledge_write`/`knowledge_read` — the fs tools do not write the vault.
