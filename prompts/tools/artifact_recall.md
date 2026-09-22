Read a stored artifact: the full output of an earlier tool call that was too long to keep in the conversation.

Arguments:
- `id` (string, required): the artifact id from the `[artifact …]` footer of the earlier result.
- `offset` (integer, optional, default 0): character offset to start at.
- `limit` (integer, optional): how many characters to return. The default, and the maximum, is the configured tool output cap; a larger request is clamped to it.

Returns that slice in full, with a footer naming the next offset when more remains. The footer's offset is exact: pass it back to continue. Page through an artifact rather than asking for everything: artifacts can be very large.
