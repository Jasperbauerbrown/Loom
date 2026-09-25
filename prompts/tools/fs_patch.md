Apply a unified diff across one or more files in a single call.

Arguments:
- `patch` (string, required): the diff. Each file section is `--- a/path`, `+++ b/path`, then one or more `@@` hunks whose lines start with a space (context), `-` (removed) or `+` (added). `/dev/null` on the left creates the file, on the right deletes it; different paths on the two sides rename it.
- `cwd` (string, optional): the directory the patch's relative paths resolve against. Defaults to the first allowed root.
- `strip` (integer, optional): path components to drop from every path, like `patch -pN`. By default only a leading `a/` or `b/` is dropped.
- `dry_run` (boolean, optional, default false): check that every file applies and report what it would do, writing nothing.

Every path the diff names is guarded like any other path, and every file it changes must have been read in this conversation (`fs_read`) and not have changed on disk since — a stale file is refused, naming which one, and you must read it again and rebuild the patch. A hunk is matched by its exact text: the context and removed lines must appear in the file, once; the `@@` line numbers are tried first as a hint but are never a fuzz factor, and the old/new text must not include `fs_read`'s `N\t` line-number prefixes. Nothing is written unless every file in the patch applies cleanly, so a patch that fails on any one file leaves the whole tree untouched. Prefer `fs_edit` for one file with a couple of changes; use `fs_patch` when a change spans several files or you already have a diff in hand.
