"""MCP servers and native connectors (03 §12.1, §13.2 Integrations).

Every field of both row types is editable here, plus the three actions the Settings
screen needs — test connection, refresh tools, reload — so nothing about an integration
is reachable only by editing a file (charter §2.2).

Mutations emit `mcp.status` / `connector.status` with the row before returning; the
connector rows carry `config_schema`, `description` and the tool names so the form
renders from one response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...connectors.base import ConnectorError
from ..deps import get_app

router = APIRouter(tags=["integrations"])


# ---------------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------------
class McpServerIn(BaseModel):
    name: str = Field(min_length=1)
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpServerPatch(BaseModel):
    name: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    enabled: bool | None = None


class ConnectorPatch(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None


# ---------------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------------
@router.get("/mcp-servers")
async def list_servers(app=Depends(get_app)) -> list[dict[str, Any]]:
    return app.mcp.rows()


@router.post("/mcp-servers", status_code=201)
async def create_server(body: McpServerIn, app=Depends(get_app)) -> dict[str, Any]:
    try:
        return await app.mcp.create(**body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/mcp-servers/{server_id}")
async def get_server(server_id: str, app=Depends(get_app)) -> dict[str, Any]:
    return _server(app, server_id)


@router.patch("/mcp-servers/{server_id}")
async def update_server(server_id: str, body: McpServerPatch, app=Depends(get_app)) -> dict[str, Any]:
    _server(app, server_id)
    fields = body.model_dump(exclude_unset=True)
    try:
        row = await app.mcp.update(server_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return row or _server(app, server_id)


@router.delete("/mcp-servers/{server_id}")
async def delete_server(server_id: str, app=Depends(get_app)) -> dict[str, Any]:
    if not await app.mcp.delete(server_id):
        raise HTTPException(404, f"mcp server {server_id} not found")
    return {"deleted": server_id}


@router.post("/mcp-servers/{server_id}/refresh")
async def refresh_server(server_id: str, app=Depends(get_app)) -> dict[str, Any]:
    _server(app, server_id)
    return await app.mcp.refresh(server_id) or _server(app, server_id)


@router.post("/mcp-servers/{server_id}/reload")
async def reload_server(server_id: str, app=Depends(get_app)) -> dict[str, Any]:
    _server(app, server_id)
    return await app.mcp.reload(server_id) or _server(app, server_id)


@router.post("/mcp-servers/{server_id}/test")
async def test_server(server_id: str, app=Depends(get_app)) -> dict[str, Any]:
    _server(app, server_id)
    return await app.mcp.test(server_id)


def _server(app, server_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    row = app.mcp.row(server_id)
    if row is None:
        raise HTTPException(404, f"mcp server {server_id} not found")
    return row


# ---------------------------------------------------------------------------------
# native connectors
# ---------------------------------------------------------------------------------
@router.get("/connectors")
async def list_connectors(app=Depends(get_app)) -> list[dict[str, Any]]:
    return app.connectors.status()


@router.get("/connectors/{name}")
async def get_connector(name: str, app=Depends(get_app)) -> dict[str, Any]:
    return _connector(app, name)


@router.patch("/connectors/{name}")
async def update_connector(name: str, body: ConnectorPatch, app=Depends(get_app)) -> dict[str, Any]:
    _connector(app, name)
    fields = body.model_dump(exclude_unset=True)
    try:
        row = None
        if "config" in fields and fields["config"] is not None:
            row = await app.connectors.set_config(name, fields["config"])
        if "enabled" in fields and fields["enabled"] is not None:
            row = await app.connectors.enable(name, bool(fields["enabled"]))
    except ConnectorError as exc:
        raise HTTPException(400, str(exc)) from exc
    return row or _connector(app, name)


@router.post("/connectors/{name}/reload")
async def reload_connector(name: str, app=Depends(get_app)) -> dict[str, Any]:
    _connector(app, name)
    return await app.connectors.reload(name) or _connector(app, name)


def _connector(app, name: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    row = app.connectors.describe(name)
    if row is None:
        raise HTTPException(404, f"connector {name} not found")
    return row
