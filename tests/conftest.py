import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from highloads_applications.configuration import load_config


@pytest.fixture
def config():
    config = load_config()
    return replace(
        config,
        balancer=replace(
            config.balancer,
            health_interval=0.01,
            health_timeout=0.05,
            request_timeout=0.3,
            shutdown_timeout=0.1,
        ),
    )


@asynccontextmanager
async def serve(app: web.Application):
    async with TestServer(app, handler_cancellation=True) as server:
        yield server


async def eventually(condition: Callable[[], bool], deadline: float = 2) -> None:
    async with asyncio.timeout(deadline):
        while not condition():  # noqa: ASYNC110 - observe external server state
            await asyncio.sleep(0.001)


@asynccontextmanager
async def stub_backend(
    config, handler: Callable[[web.Request], Awaitable[web.StreamResponse]], health=None
):
    async def healthy(request):
        return web.Response()

    app = web.Application()
    app.router.add_get("/work", handler)
    app.router.add_get("/health", health or healthy)
    async with serve(app) as server:
        yield replace(config.backends[0], port=server.port, delay_ms=0), server
