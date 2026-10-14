Search the web and get back the top results: title, URL and snippet.

Arguments:
- `query` (string, required): what to search for. Write it as you would type it into a search box — keywords and distinctive phrases, not a question to the model.
- `k` (integer, optional): how many results to return, up to 20. Defaults to the configured number.

Returns a numbered list; snippets are short by design. Follow a result with `http` (deferred: load it with `tool_search`; `format: "markdown"`) to read the page itself — the snippet is only enough to choose. The search runs against whichever provider the user has configured (a self-hosted SearXNG, or Brave, Exa or Tavily); if none is configured the tool says so and tells you what to ask for.
