Load a procedure — a stored how-to that you wrote or the user installed, kept out of
context until it is needed. The procedures available to you are listed in your context
by name and description; this returns the full body of one.

Arguments:
- `name` (required): the procedure's name exactly as the list shows it.

Returns the procedure's instructions. Any scripts it ships stay on disk: run them with
`shell` at the paths the procedure names rather than reading them into context.

A procedure that arrived from outside Loom stays unloadable until the user approves it —
if you get that error, say so and move on; do not try to read the file another way.
