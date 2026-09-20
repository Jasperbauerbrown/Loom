Read a stored artifact: the full output of an earlier tool call that was too long to keep in the conversation.

Arguments:
- `id` (string, required): the artifact id from the `[artifact …]` footer of the earlier result.
- `offset` (integer, optional, default 0): character offset to start at.
- `limit` (integer, optional, default 8000): how many characters to return.

Returns that slice, with a footer naming the next offset when more remains. Page through it rather than asking for everything: artifacts can be very large.
