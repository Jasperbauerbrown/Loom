Read a text file from the local filesystem, whole or as a slice of lines.

Arguments:
- `path` (string, required): absolute path, or relative to the first allowed root. Must be inside an allowed root.
- `offset` (integer, optional, default 0): zero-based line to start at. The footer of a truncated read tells you the offset to pass next.
- `limit` (integer, optional, default 400): how many lines to return.

Returns the lines as plain text. When more of the file remains, the last line is a footer naming the lines shown, the total, and the next offset. Output is capped at 8,000 characters regardless of `limit`. An image file (png, jpeg, webp, gif) is returned as the image itself, whole — `offset` and `limit` do not apply, and files over the configured image size cap are refused. Other binary files are refused — read them with `shell` if you truly need to.
