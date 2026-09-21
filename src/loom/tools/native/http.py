"""The `http` tool (03 §9.2).

One request, one readable answer: a status line, the headers that matter, and the body
as text with JSON pretty-printed so the model can read a field without a second call.

GET and HEAD are read-only *for these arguments* — `read_only_for` says so, and the
permission layer grants them without spending a classifier call. Everything else changes
something at the other end and is judged.

Network failures are results, not exceptions: an unreachable host is information the
model can act on (try again, try another URL, tell the user).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ... import prompts
from ...artifacts import IMAGE_MIMES, image_dimensions, sniff_image
from ..registry import Tool, ToolContext, ToolResult

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
INTERESTING_HEADERS = (
    "content-type",
    "content-length",
    "location",
    "retry-after",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "date",
)
BODY_CAP = 200_000


def client_factory(timeout: float, follow_redirects: bool) -> httpx.AsyncClient:
    """Seam for tests: monkeypatch this to hand back a client on a MockTransport."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


def _head_lines(response: httpx.Response) -> list[str]:
    content_type = response.headers.get("content-type", "")
    lines = [f"HTTP {response.status_code} {content_type}".rstrip()]
    for name in INTERESTING_HEADERS:
        if name in response.headers and name != "content-type":
            lines.append(f"{name}: {response.headers[name]}")
    return lines


def _image_mime(response: httpx.Response) -> str | None:
    """The response's image type when it is one: sniffed from the bytes first (servers
    mislabel), the declared content-type second."""
    body = response.content or b""
    sniffed = sniff_image(body[:16])
    if sniffed:
        return sniffed
    declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return declared if declared in IMAGE_MIMES else None


def _format_image(response: httpx.Response, mime: str, cap: int) -> ToolResult:
    body = response.content or b""
    lines = _head_lines(response)
    if len(body) > cap:
        return ToolResult.error(
            "\n".join(lines) + f"\n\nimage is {len(body)} bytes, over the {cap // (1024 * 1024)} MB image cap (conversation.image_max_mb)",
            status=response.status_code, url=str(response.url),
        )
    dims = image_dimensions(body)
    shape = f", {dims[0]}×{dims[1]}" if dims else ""
    return ToolResult(
        content="\n".join(lines) + f"\n\n[image: {mime}{shape}, {len(body)} bytes]",
        ok=response.status_code < 400,
        meta={"status": response.status_code, "url": str(response.url), "mime": mime, "bytes": len(body)},
        images=[{"data": body, "mime": mime}],
    )


def _format(response: httpx.Response) -> str:
    lines = _head_lines(response)
    content_type = response.headers.get("content-type", "")
    body = response.text or ""
    if "json" in content_type.lower() and body.strip():
        try:
            body = json.dumps(response.json(), indent=2, ensure_ascii=False)
        except Exception:
            pass
    if len(body) > BODY_CAP:
        body = body[:BODY_CAP] + f"\n[body truncated at {BODY_CAP} chars]"
    return "\n".join(lines) + "\n\n" + body


async def http_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    method = str(args.get("method") or "GET").upper()
    if method not in METHODS:
        return ToolResult.error(f"unsupported method {method}; use one of {', '.join(METHODS)}")
    url = str(args.get("url") or "").strip()
    if not url:
        return ToolResult.error("url is empty")
    headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()} if isinstance(args.get("headers"), dict) else {}
    timeout = float(args.get("timeout_s") or ctx.app.settings.get("conversation.tool_timeout_http_s"))
    follow = bool(args.get("follow_redirects", True))

    body = args.get("body")
    kwargs: dict[str, Any] = {"headers": headers}
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif isinstance(body, str) and body:
        kwargs["content"] = body.encode("utf-8")

    client = client_factory(timeout, follow)
    try:
        async with client:
            response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        return ToolResult.error(f"{method} {url} failed: {type(exc).__name__}: {exc}")
    except (ValueError, OSError) as exc:
        return ToolResult.error(f"{method} {url} failed: {exc}")
    mime = _image_mime(response)
    if mime:
        cap = int(ctx.app.settings.get("conversation.image_max_mb")) * 1024 * 1024
        return _format_image(response, mime, cap)
    return ToolResult(
        content=_format(response),
        ok=response.status_code < 400,
        meta={"status": response.status_code, "url": str(response.url)},
    )


def tools() -> list[Tool]:
    return [
        Tool(
            name="http",
            description=prompts.load("tools/http"),
            parameters={
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": list(METHODS), "default": "GET"},
                    "url": {"type": "string", "description": "Absolute URL."},
                    "headers": {"type": "object", "description": "Request headers; values may use {{secret:NAME}}."},
                    "body": {"description": "Request body: a string, or an object sent as JSON."},
                    "timeout_s": {"type": "number"},
                    "follow_redirects": {"type": "boolean", "default": True},
                },
                "required": ["method", "url"],
            },
            handler=http_handler,
            read_only_for=lambda a: str(a.get("method", "GET")).upper() in ("GET", "HEAD"),
        )
    ]
