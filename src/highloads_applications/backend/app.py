import asyncio
import ipaddress
import logging
from typing import TypedDict

from aiohttp import web

from highloads_applications.configuration import (
    BackendConfig,
    ConfigurationError,
    number,
)
from highloads_applications.http_helpers import request_id

logger = logging.getLogger(__name__)


class BackendState(TypedDict):
    delay_ms: float
    available: bool
    work_status: int


def create_app(config: BackendConfig) -> web.Application:
    state: BackendState = {
        "delay_ms": config.delay_ms,
        "available": True,
        "work_status": 200,
    }

    async def work(request: web.Request) -> web.Response:
        value = request_id(request, allow_probe=True)

        if not state["available"]:
            raise web.HTTPServiceUnavailable(text="Backend unavailable")

        # Изменение настроек не должно влиять на уже принятые запросы.
        delay = state["delay_ms"]
        status = state["work_status"]

        await asyncio.sleep(delay / 1000)

        logger.info(
            "work request_id=%s backend=%s probe=%s status=%s",
            value,
            config.id,
            value == 0,
            status,
        )

        return web.json_response({"request_id": value, "backend_id": config.id}, status=status)

    async def health(request: web.Request) -> web.Response:
        if state["available"]:
            return web.Response(status=200)

        return web.Response(status=503)

    async def admin(request: web.Request) -> web.Response:
        if not request.remote or not ipaddress.ip_address(request.remote).is_loopback:
            raise web.HTTPForbidden(text="Loopback clients only")

        try:
            payload = await request.json()
        except ValueError as error:
            raise web.HTTPBadRequest(text="Expected a JSON object") from error

        if (
            not isinstance(payload, dict)
            or not payload
            or payload.keys() - {"delay_ms", "available", "work_status"}
        ):
            raise web.HTTPBadRequest(text="Expected delay_ms, available and/or work_status")

        delay = state["delay_ms"]

        if "delay_ms" in payload:
            try:
                delay = number(payload["delay_ms"], "delay_ms", zero=True)
            except ConfigurationError as error:
                raise web.HTTPBadRequest(text=str(error)) from error

        available = payload.get("available", state["available"])
        status = payload.get("work_status", state["work_status"])

        if type(available) is not bool or type(status) is not int or status not in {200, 500}:
            raise web.HTTPBadRequest(
                text="available must be boolean; work_status must be 200 or 500"
            )

        # Меняем состояние только после проверки всех полей.
        state.update({"delay_ms": delay, "available": available, "work_status": status})
        logger.info("admin backend=%s state=%s", config.id, state)
        return web.json_response(state)

    async def startup(app: web.Application) -> None:
        logger.info("backend_start id=%s", config.id)

    async def cleanup(app: web.Application) -> None:
        logger.info("backend_stop id=%s", config.id)

    app = web.Application()
    app.router.add_get("/work", work)
    app.router.add_get("/health", health)
    if ipaddress.ip_address(config.host).is_loopback:
        app.router.add_post("/admin/state", admin)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
