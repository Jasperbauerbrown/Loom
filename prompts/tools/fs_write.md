Create a file or overwrite it completely with new content.

Arguments:
- `path` (string, required): the file to write. Must be inside an allowed root.
- `content` (string, required): the whole new contents of the file.
- `create_dirs` (boolean, optional, default true): create missing parent directories.

Returns the number of bytes written. This replaces the entire file — to change part of an existing file use `fs_edit`, which cannot clobber work you have not read.
