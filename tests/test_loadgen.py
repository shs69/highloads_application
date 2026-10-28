import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest
from aiohttp import web
from conftest import serve, stub_backend

from highloads_applications.backend.app import create_app as backend_app
from highloads_applications.balancer.app import RUNTIME, create_app
from highloads_applications.loadgen.__main__ import run_until_signal
from highloads_applications.loadgen.generator import generate


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
async def test_600_запросов_к_трём_бэкендам_и_полная_статистика(config, tmp_path, mode):
    async with AsyncExitStack() as stack:
        backends = []
        for backend in config.backends:
            server = await stack.enter_async_context(
                serve(backend_app(replace(backend, delay_ms=0)))
            )
            backends.append(replace(backend, port=server.port, delay_ms=0))
        app = create_app(replace(config, backends=tuple(backends)))
        server = await stack.enter_async_context(serve(app))
        settings = replace(
            config.loadgen,
            url=str(server.make_url("")),
            requests=600,
            mode=mode,
            concurrency=20,
            output=str(tmp_path / "load.json"),
        )
        report = await generate(settings)
        summary = report["summary"]
        rows = report["requests"]
        assert summary["sent"] == summary["successful"] == 600
        assert summary["http_errors"] == summary["client_errors"] == {}
        assert summary["assignments_by_backend"] == summary["successful_by_backend"]
        assert set(summary["assignments_by_backend"]) == {"A", "B", "C"}
        assert sum(summary["assignments_by_backend"].values()) == 600
        assert summary["requests_per_second"] > 0
        assert [row["request_id"] for row in rows] == list(range(1, 601))
        assert sorted(row["assignment_number"] for row in rows) == list(range(1, 601))
        exploration = sorted(
            (row for row in rows if row["selection_mode"] == "exploration"),
            key=lambda row: row["assignment_number"],
        )
        assert [row["backend_id"] for row in exploration] == ["A", "B", "C"] * 20
        latency = summary["latency_seconds"]
        assert (
            latency["min"] <= latency["p50"] <= latency["p95"] <= latency["p99"] <= latency["max"]
        )
        assert app[RUNTIME].algorithm.assignments == 600


async def test_назначения_с_ошибками_отличаются_от_успехов(config):
    async def handler(request):
        status = 500 if request.query["request_id"] == "1" else 200
        return web.json_response({"backend_id": "A"}, status=status)

    async with stub_backend(config, handler) as (backend, _):
        app = create_app(replace(config, backends=(backend,)))
        async with serve(app) as server:
            report = await generate(
                replace(config.loadgen, url=str(server.make_url("")), requests=2)
            )
    summary = report["summary"]
    assert summary["assignments_by_backend"] == {"A": 2}
    assert summary["successful_by_backend"] == {"A": 1}
    assert summary["http_errors"] == {"500": 1}
    assert summary["latency_seconds"]["p95"] is None


async def test_предел_конкурентности_генератора(config):
    active = maximum = count = 0
    release = asyncio.Event()
    entered = asyncio.Event()

    async def handler(request):
        nonlocal active, maximum, count
        active += 1
        count += 1
        maximum = max(maximum, active)
        if active == 3:
            entered.set()
        try:
            await release.wait()
            return web.Response()
        finally:
            active -= 1

    async with stub_backend(config, handler) as (_, server):
        async with asyncio.TaskGroup() as group:
            task = group.create_task(
                generate(
                    replace(
                        config.loadgen,
                        url=str(server.make_url("")),
                        requests=20,
                        mode="concurrent",
                        concurrency=3,
                    )
                )
            )
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                assert count == 3
            finally:
                release.set()
        assert maximum == 3
        assert task.result()["summary"]["sent"] == 20


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
async def test_сигнал_отменяет_генератор_и_закрывает_соединения(config, monkeypatch, mode):
    entered, disconnected, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    callbacks = []
    removed = []
    active = 0
    expected = 1 if mode == "sequential" else 3
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: callbacks.append(callback)
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))

    async def handler(request):
        nonlocal active
        active += 1
        if active == expected:
            entered.set()
        try:
            await blocked.wait()
        finally:
            active -= 1
            if active == 0:
                disconnected.set()
        return web.Response()

    async with stub_backend(config, handler) as (_, server):
        task = asyncio.create_task(
            run_until_signal(
                replace(
                    config.loadgen,
                    url=str(server.make_url("")),
                    requests=20,
                    mode=mode,
                    concurrency=3,
                )
            )
        )
        try:
            async with asyncio.timeout(1):
                await entered.wait()
            callbacks[0]()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(1):
                await disconnected.wait()
            assert len(removed) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
