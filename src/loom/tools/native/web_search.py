"""The `web_search` tool (03 §9.2).

Searching the web is not something a machine can do on its own: it needs somebody's
index. So this tool is a thin, uniform front for four of them — a self-hosted SearXNG,
or Brave, Exa or Tavily with an API key — chosen by one setting. Until that setting is
set the tool still exists and still answers; it answers with the "not configured" note
that tells the model exactly where the person has to go. A tool that vanishes when it is
unconfigured teaches the model nothing.

Each provider differs only in how a query becomes a request and how a payload becomes
rows, so that is all a provider is here: a `_request` branch and a `_parse` branch over
one `Hit`. Everything after — the key lookup, the timeout, the failure classes, the
numbered output — is shared, because none of it has any business differing.

The key never passes through the model. The setting holds the *name* of a secret; the
handler fetches the value from the store immediately before the call, the way the
dispatcher does for `{{secret:NAME}}`.

Read-only by construction: a search changes nothing at the other end, so the permission
layer's default (`read_only` → `allow`) is the policy we want, and no bootstrap row is
needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from ... import prompts
from ..registry import Tool, ToolContext, ToolResult

PROVIDERS = ("searxng", "brave", "exa", "tavily")
KEYED = ("brave", "exa", "tavily")  # providers that need the stored API key
MAX_RESULTS = 20
SNIPPET_CAP = 400

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
EXA_URL = "https://api.exa.ai/search"
TAVILY_URL = "https://api.tavily.com/search"


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


def client_factory(timeout: float) -> httpx.AsyncClient:
    """Seam for tests: monkeypatch this to hand back a client on a MockTransport."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


def _request(provider: str, query: str, k: int, base_url: str, key: str) -> dict[str, Any]:
    """The one request this provider wants, as kwargs for `client.request`."""
    if provider == "searxng":
        return {
            "method": "GET",
            "url": base_url.rstrip("/") + "/search",
            "params": {"q": query, "format": "json"},
            "headers": {"Accept": "application/json"},
        }
    if provider == "brave":
        return {
            "method": "GET",
            "url": BRAVE_URL,
            "params": {"q": query, "count": k},
            "headers": {"Accept": "application/json", "X-Subscription-Token": key},
        }
    if provider == "exa":
        return {
            "method": "POST",
            "url": EXA_URL,
            "json": {"query": query, "numResults": k, "contents": {"text": {"maxCharacters": SNIPPET_CAP}}},
            "headers": {"Accept": "application/json", "x-api-key": key},
        }
    return {
        "method": "POST",
        "url": TAVILY_URL,
        "json": {"query": query, "max_results": k},
        "headers": {"Accept": "application/json", "Authorization": f"Bearer {key}"},
    }


def _rows(provider: str, payload: Any) -> list[dict[str, Any]]:
    """Where this provider keeps its result list."""
    if not isinstance(payload, dict):
        return []
    if provider == "brave":
        web = payload.get("web")
        rows = web.get("results") if isinstance(web, dict) else None
    else:
        rows = payload.get("results")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _parse(provider: str, payload: Any, k: int) -> list[Hit]:
    """Provider payload → hits. Field names differ; the shape does not. A row with no
    URL is dropped: a result the model cannot follow is noise."""
    snippet_keys = {
        "searxng": ("content", "snippet"),
        "brave": ("description", "snippet"),
        "exa": ("text", "summary", "snippet"),
        "tavily": ("content", "snippet"),
    }[provider]
    hits: list[Hit] = []
    for row in _rows(provider, payload):
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        snippet = ""
        for name in snippet_keys:
            value = row.get(name)
            if isinstance(value, str) and value.strip():
                snippet = " ".join(value.split())
                break
        hits.append(Hit(title=str(row.get("title") or "").strip() or url, url=url, snippet=snippet[:SNIPPET_CAP]))
        if len(hits) >= k:
            break
    return hits


def _render(query: str, provider: str, hits: list[Hit]) -> str:
    lines = [f'{len(hits)} results for "{query}" ({provider})', ""]
    for n, hit in enumerate(hits, 1):
        lines.append(f"{n}. {hit.title}")
        lines.append(f"   {hit.url}")
        if hit.snippet:
            lines.append(f"   {hit.snippet}")
    return "\n".join(lines)


def _failure(provider: str, response: httpx.Response) -> ToolResult:
    """A provider that answered with a status, not results: the status and the first
    line of the body, which is where every one of them puts the reason."""
    first = ""
    try:
        first = (response.text or "").strip().splitlines()[0][:200]
    except Exception:  # pragma: no cover - a body that cannot be decoded at all
        first = ""
    detail = f": {first}" if first else ""
    return ToolResult.error(
        f"{provider} search failed: HTTP {response.status_code}{detail}",
        status=response.status_code, provider=provider,
    )


async def web_search_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult.error("query is empty")

    settings = ctx.app.settings
    provider = str(settings.get("tools.web_search_provider") or "").strip().lower()
    if provider not in PROVIDERS:
        return ToolResult.error(prompts.load("context/web-search-unconfigured"), provider=provider)

    k = int(args.get("k") or settings.get("tools.web_search_results") or 5)
    k = max(1, min(k, MAX_RESULTS))

    base_url = str(settings.get("tools.web_search_url") or "").strip()
    if provider == "searxng" and not base_url:
        return ToolResult.error("searxng is selected but no instance URL is set (Settings → Tools)", provider=provider)

    key = ""
    if provider in KEYED:
        name = str(settings.get("tools.web_search_secret") or "web_search").strip()
        store = ctx.app.secrets
        key = (store.get(name) if store is not None else None) or ""
        if not key:
            return ToolResult.error(
                f"no secret named {name!r}: store the {provider} API key with secret_set, "
                "or name a different secret in Settings → Tools",
                provider=provider,
            )

    timeout = float(settings.get("conversation.tool_timeout_http_s"))
    request = _request(provider, query, k, base_url, key)
    client = client_factory(timeout)
    try:
        async with client:
            response = await client.request(**request)
    except httpx.HTTPError as exc:
        return ToolResult.error(f"{provider} search failed: {type(exc).__name__}: {exc}", provider=provider)
    except (ValueError, OSError) as exc:
        return ToolResult.error(f"{provider} search failed: {exc}", provider=provider)

    if response.status_code >= 400:
        return _failure(provider, response)
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return ToolResult.error(
            f"{provider} search returned a body that is not JSON (HTTP {response.status_code})",
            status=response.status_code, provider=provider,
        )

    hits = _parse(provider, payload, k)
    if not hits:
        return ToolResult(content=f'no results for "{query}" ({provider})', meta={"count": 0, "provider": provider})
    return ToolResult(content=_render(query, provider, hits), meta={"count": len(hits), "provider": provider})


def tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=prompts.load("tools/web_search"),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_RESULTS,
                        "description": "How many results to return; defaults to the configured number.",
                    },
                },
                "required": ["query"],
            },
            handler=web_search_handler,
            read_only=True,
        )
    ]
