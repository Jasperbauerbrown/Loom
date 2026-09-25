Create a file or overwrite it completely with new content.

Arguments:
- `path` (string, required): the file to write. Must be inside an allowed root.
- `content` (string, required): the whole new contents of the file.
- `create_dirs` (boolean, optional, default true): create missing parent directories.

Returns the number of bytes written. This replaces the entire file — to change part of an existing file use `fs_edit`, which cannot clobber work you have not read. Overwriting an existing file needs this conversation to have read it first (`fs_read`); if it changed on disk since that read, the write is refused and you must read it again before retrying — the user edits these files elsewhere at the same time, so a blind overwrite could throw away their changes. Creating a new file needs no read. An existing file's line endings (CRLF or LF) and UTF-8 BOM are preserved: write `content` with plain `\n` and Loom puts the file's own endings back. A relative path that names an existing vault note is refused with a pointer to `knowledge_write`/`knowledge_read` — the fs tools do not write the vault.
