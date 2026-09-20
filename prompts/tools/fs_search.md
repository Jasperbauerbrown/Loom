Search file contents with a regular expression and report matches with surrounding lines.

Arguments:
- `pattern` (string, required): a Python regular expression.
- `path` (string, required): file or directory to search. Must be inside an allowed root.
- `glob` (string, optional, default `**/*`): which files under `path` to search.
- `context` (integer, optional, default 2): lines of context shown either side of a match.
- `max_results` (integer, optional, default 100): stop after this many matches.
- `case_sensitive` (boolean, optional, default false): match case exactly.

Returns `path:line: text` blocks separated by `--`. Binary files and files over 2 MB are skipped. Use this to locate code or notes before reading a whole file.
