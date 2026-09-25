Read a text file from the local filesystem, whole or as a slice of lines.

Arguments:
- `path` (string, required): absolute path, or relative to the first allowed root. Must be inside an allowed root.
- `offset` (integer, optional, default 0): zero-based line to start at. The footer of a truncated read tells you the offset to pass next.
- `limit` (integer, optional, default 400): how many lines to return.
- `line_numbers` (boolean, optional, default true): prefix each line with its number and a tab (`N\t`), numbered absolutely so a later page continues the same count. The prefix is not part of the file's text: never include it in `fs_edit`'s `old`/`new` or in an `fs_patch` diff. Pass false when you are going to quote the file's prose verbatim.
- `layout` (boolean, optional, default true): PDFs only — keep the page layout (`pdftotext -layout`) rather than reflowing text into a single stream.

Returns the lines as plain text. When more of the file remains, the last line is a footer naming the lines shown, the total, and the next offset; one call returns at most one page, the configured tool output cap in characters, regardless of `limit`, and the footer's offset is exact, so pass it back to continue. A PDF is read as text through the `pdftotext` binary when it is on PATH — the error names the binary when it is not — and paged like any text file. A `.ipynb` notebook is rendered as its cells, one block per cell with the source and a summarised line per output, and paged the same way; to change a cell, use `fs_edit` against its source. An image file (png, jpeg, webp, gif) is returned as the image itself, whole — `offset` and `limit` do not apply, and files over the configured image size cap are refused. Other binary files are refused — read them with `shell` if you truly need to.
