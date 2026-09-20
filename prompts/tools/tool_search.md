Find tools that are not currently in your tool list and load their definitions for the rest of this turn.

Arguments:
- `query` (string, required): words describing what you want to do, e.g. "create a calendar event".
- `k` (integer, optional, default 10): how many tool definitions to return.

Returns the matching tools with their arguments; they become callable immediately. Use this when the capability you need plainly exists on this machine but is not in front of you — connector and MCP tools are hidden behind this search when there are too many to show at once.
