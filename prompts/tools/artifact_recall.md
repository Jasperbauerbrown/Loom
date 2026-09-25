Read a stored artifact or a stored image: the full output of an earlier tool call that was too long to keep in the conversation, or an image kept out of the transcript. Visible ids are those of this conversation, or of any thread this conversation spawned (and their sub-threads, however deep) — never a sibling thread's, never the conversation that spawned this one.

Arguments:
- `id` (string, required): the artifact id from the `[artifact …]` footer of the earlier result, or an image id from an `[image … attached to this result]` marker.
- `offset` (integer, optional, default 0): character offset to start at. Meaningless for an image.
- `limit` (integer, optional): how many characters to return. The default, and the maximum, is the configured tool output cap; a larger request is clamped to it. Meaningless for an image.

For a text artifact: returns that slice in full, with a footer naming the next offset when more remains. The footer's offset is exact: pass it back to continue. Page through an artifact rather than asking for everything: artifacts can be very large.

For an image id: returns the image itself, attached to the result — `offset` and `limit` are ignored and the result says so. An id outside this conversation's spawn tree, or one that does not exist at all, is refused the same way either way.
