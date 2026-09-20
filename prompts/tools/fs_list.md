List files and directories under a path.

Arguments:
- `path` (string, required): the directory to list. Must be inside an allowed root.
- `pattern` (string, optional, default `**/*`): a glob relative to `path`. Use `*` for the immediate children only, `**/*.py` for a recursive type filter.
- `max_results` (integer, optional, default 500): stop after this many entries; the footer says whether more were dropped.
- `include_hidden` (boolean, optional, default false): include dot-files and dot-directories.

Returns one path per line, relative to `path`, directories marked with a trailing `/`. Prefer this over a shell `ls`: it is read-only, needs no approval, and returns a stable format.
