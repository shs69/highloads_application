import asyncio
import logging

import pytest
from aiohttp import ClientSession
from conftest import serve

from highloads_applications.backend import app as backend_app
from highloads_applications.backend.app import create_app


async def test_задержка_конкурентность_и_независимый_health(config, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=backend_app.__name__)
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()
    delays = []

    async def controlled_sleep(seconds):
        nonlocal entered
        delays.append(seconds)
        entered += 1
        if entered == 3:
            all_entered.set()
        await release.wait()

    # Меняем только используемый модулем объект, не глобальную asyncio.sleep.
    class Clock:
        sleep = staticmethod(controlled_sleep)

    monkeypatch.setattr(backend_app, "asyncio", Clock)
    async with serve(create_app(config.backends[0])) as server, ClientSession() as client:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(client.get(server.make_url(f"/work?request_id={i}")))
                for i in range(1, 4)
            ]
            try:
                async with asyncio.timeout(1):
                    await all_entered.wait()
                    async with client.get(server.make_url("/health")) as response:
                        assert response.status == 200
            finally:
                release.set()
        assert delays == [0.05] * 3
        for i, task in enumerate(tasks, 1):
            async with task.result() as response:
                assert response.status == 200
                assert await response.json() == {"request_id": i, "backend_id": "A"}
    assert "backend_start id=A" in caplog.messages
    assert "backend_stop id=A" in caplog.messages
    assert sum(message.startswith("work request_id=") for message in caplog.messages) == 3


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?request_id=x",
        "?request_id=-1",
        "?request_id=1.5",
        "?request_id=1&request_id=2",
        "?request_id=١",
    ],
)
async def test_некорректный_request_id(config, query):
    async with serve(create_app(config.backends[0])) as server, ClientSession() as client:
        async with client.get(server.make_url("/work" + query)) as response:
            assert response.status == 400


async def test_управление_отказом_восстановлением_и_http_500(config):
    app = create_app(config.backends[1])
    async with serve(app) as server, ClientSession() as client:

        async def update(payload):
            async with client.post(server.make_url("/admin/state"), json=payload) as response:
                return response.status, await response.json()

        status, state = await update({"delay_ms": 500, "available": False})
        assert status == 200 and state["delay_ms"] == 500
        for route in ("/health", "/work?request_id=1"):
            async with client.get(server.make_url(route)) as response:
                assert response.status == 503
        await update({"available": True, "delay_ms": 0, "work_status": 500})
        async with client.get(server.make_url("/work?request_id=1")) as response:
            assert response.status == 500
            assert (await response.json())["backend_id"] == "B"
        async with client.get(server.make_url("/health")) as response:
            assert response.status == 200
        await update({"work_status": 200})
        async with client.get(server.make_url("/work?request_id=0")) as response:
            assert response.status == 200
        async with client.post(server.make_url("/admin/state"), json={"delay_ms": -1}) as response:
            assert response.status == 400
        _, state = await update({"available": True})
        assert state["delay_ms"] == 0


async def test_админский_маршрут_отсутствует_на_внешнем_интерфейсе(config):
    from dataclasses import replace

    app = create_app(replace(config.backends[0], host="0.0.0.0"))
    async with serve(app) as server, ClientSession() as client:
        async with client.post(
            server.make_url("/admin/state"), json={"available": False}
        ) as response:
            assert response.status == 404
