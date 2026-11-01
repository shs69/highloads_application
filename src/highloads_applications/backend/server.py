import asyncio
import uuid
from typing import TypedDict

from aiohttp import web

from highloads_applications.configuration import BackendConfig


class BackendState(TypedDict):
    delay_ms: float
    available: bool


def create_server(*, backend: BackendConfig) -> web.Application:
    routes = web.RouteTableDef()
    state: BackendState = {
        "delay_ms": backend.delay_ms,
        "available": True,
    }

    @routes.get("/work")
    async def work(request: web.Request):
        if not state["available"]:
            raise web.HTTPServiceUnavailable()

        delay = state["delay_ms"]

        await asyncio.sleep(delay / 1000)

        return web.json_response(
            {"backend_id": backend.id, "request_id": str(uuid.uuid4())}
        )

    @routes.get("/health")
    async def health(_request: web.Request):
        return web.json_response(
            {"backend_id": backend.id, "request_id": str(uuid.uuid4())}
        )

    app = web.Application()
    _ = app.add_routes(routes)

    return app
