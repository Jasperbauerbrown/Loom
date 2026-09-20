Replace an exact piece of text in a file.

Arguments:
- `path` (string, required): the file to edit. Must be inside an allowed root.
- `old` (string, required): the exact text to find, including indentation and line breaks. It must occur exactly once unless `replace_all` is true; if it occurs more often, include more surrounding context.
- `new` (string, required): the text to put in its place. An empty string deletes the matched text.
- `replace_all` (boolean, optional, default false): replace every occurrence instead of requiring a unique one.

Returns the number of replacements and a short diff of what changed. Read the file first: an edit whose `old` does not match byte for byte fails and changes nothing.
