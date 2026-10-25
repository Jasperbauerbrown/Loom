"""Router registry. Each domain package adds its APIRouter module here with a one-line
edit; server/app.py includes them in order. Module must expose `router: APIRouter`."""

ROUTERS: list[str] = [
    "loom.server.routers.system",
    "loom.server.routers.settings",
    "loom.server.routers.models",
    "loom.server.routers.conversations",
    "loom.server.routers.approvals",
    "loom.server.routers.permissions",
    "loom.server.routers.secrets",
    "loom.server.routers.items",
    "loom.server.routers.processes",
    "loom.server.routers.vault",
    "loom.server.routers.maintenance",
    "loom.server.routers.integrations",
    "loom.server.routers.schedules",
    "loom.server.routers.promote",
    "loom.server.routers.voice",
]
