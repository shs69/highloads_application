import asyncio
import logging
from typing import TypedDict

from aiohttp import web

from highloads_applications.configuration import (
    BackendConfig,
    ConfigurationError,
    number,
)
from highloads_applications.http_helpers import request_id as parse_request_id

logger = logging.getLogger(__name__)


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
        request_id = parse_request_id(request, allow_probe=True)

        logger.info(
            "Получен /work request_id=%s backend=%s",
            request_id,
            backend.id,
        )
        if not state["available"]:
            raise web.HTTPServiceUnavailable()

        delay = state["delay_ms"]

        await asyncio.sleep(delay / 1000)

        return web.json_response({"backend_id": backend.id, "request_id": request_id})

    @routes.get("/health")
    async def health(_request: web.Request):
        return web.json_response(state, status=200 if state["available"] else 503)

    @routes.get("/admin")
    async def get_state(_request: web.Request):
        return web.json_response(state)

    @routes.post("/admin")
    async def update_state(request: web.Request):
        try:
            data = await request.json()
        except ValueError as error:
            raise web.HTTPBadRequest(text="Ожидается JSON") from error

        if (
            not isinstance(data, dict)
            or not data
            or set(data) - {"delay_ms", "available"}
        ):
            raise web.HTTPBadRequest(text="Допустимы поля delay_ms и available")

        delay_ms = state["delay_ms"]
        available = state["available"]

        try:
            if "delay_ms" in data:
                delay_ms = number(data["delay_ms"], "delay_ms", zero=True)

            if "available" in data:
                if type(data["available"]) is not bool:
                    raise ConfigurationError("available: требуется true или false")

                available = data["available"]
        except ConfigurationError as error:
            raise web.HTTPBadRequest(text=str(error)) from error

        # Применяем оба поля только после проверки всего запроса.
        state.update(delay_ms=delay_ms, available=available)

        logger.info("Изменены настройки backend=%s state=%s", backend.id, state)

        return web.json_response(state)

    app = web.Application()
    _ = app.add_routes(routes)

    return app
