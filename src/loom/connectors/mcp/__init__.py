"""Scratch-built MCP client (03 §10.1): transports, client, manager.

No SDK: the protocol Loom needs is JSON-RPC 2.0 over two transports plus five methods,
which is smaller than the dependency would be (CLAUDE.md, dependency-light).
"""

from __future__ import annotations

from .client import McpClient, flatten_content
from .manager import Manager, slug
from .transports import HttpTransport, McpError, StdioTransport, build_transport

__all__ = [
    "McpClient",
    "McpError",
    "Manager",
    "StdioTransport",
    "HttpTransport",
    "build_transport",
    "flatten_content",
    "slug",
]
