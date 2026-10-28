import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError, ClientSession, web
from aiohttp.test_utils import TestServer
from conftest import eventually, serve, stub_backend

from highloads_applications.backend.app import create_app as backend_app
from highloads_applications.balancer import app as balancer_app
from highloads_applications.balancer.app import RUNTIME, Balancer, create_app


@pytest.fixture
async def balancer(config):
    async with ClientSession() as session:
        balancer = Balancer(config, session)
        for backend, measurement in zip(
            balancer.algorithm.backends, (0.05, 0.1, 0.15), strict=True
        ):
            backend.available = True
            backend.estimate = measurement
        yield balancer


async def reply(request):
    return web.json_response({"request_id": int(request.query["request_id"]), "backend_id": "A"})


async def test_проксирование_отдельные_соединения_и_новое_состояние(config):
    transports = []

    async def handler(request):
        transports.append(request.transport)
        return await reply(request)

    async with stub_backend(config, handler) as (backend, _), ClientSession() as client:
        configured = replace(
            config, backends=(backend,), balancer=replace(config.balancer, health_interval=100)
        )
        for _ in range(2):
            app = create_app(configured)
            async with serve(app) as server:
                runtime = app[RUNTIME]
                assert runtime.algorithm.assignments == 0
                assert runtime.algorithm.backends[0].available
                for value in (1, 2, 3):
                    async with client.get(server.make_url(f"/work?request_id={value}")) as response:
                        assert response.status == 200
                        assert await response.json() == {"request_id": value, "backend_id": "A"}
                        assert response.headers["X-Assignment-Number"] == str(value)
                async with client.get(server.make_url("/work?request_id=0")) as response:
                    assert response.status == 400
                assert runtime.algorithm.assignments == 3
            assert runtime.session.closed
            assert all(task.done() for task in runtime.health_tasks)
            assert not runtime.work_tasks
    assert len(transports) == 8  # Одна проба и три запроса на каждый запуск.
    assert len({id(transport) for transport in transports}) == 8
    assert all(transport.is_closing() for transport in transports)


async def test_все_недоступны_возвращает_503_и_продолжает_работать(config):
    async def failed(request):
        return web.Response(status=500)

    async with stub_backend(config, failed) as (backend, _):
        app = create_app(replace(config, backends=(backend,)))
        async with serve(app) as server, ClientSession() as client:
            async with client.get(server.make_url("/work?request_id=1")) as response:
                assert response.status == 503
            assert app[RUNTIME].algorithm.assignments == 0
            assert app[RUNTIME].algorithm.backends[0].estimate == float("inf")


@pytest.mark.parametrize(
    "error_kind, expected", [("http500", 500), ("timeout", 504), ("disconnect", 502)]
)
async def test_ошибки_без_повтора_и_без_изменения_ewma(config, error_kind, expected):
    calls = []
    blocked = asyncio.Event()

    async def handler(request):
        value = int(request.query["request_id"])
        calls.append(value)
        if value == 0:
            return await reply(request)
        if error_kind == "http500":
            return web.json_response({"error": "test"}, status=500)
        if error_kind == "timeout":
            await blocked.wait()
        else:
            request.transport.close()
        return await reply(request)

    async with stub_backend(config, handler) as (backend, _):
        configured = replace(
            config,
            backends=(backend,),
            balancer=replace(config.balancer, health_interval=100, request_timeout=0.03),
        )
        app = create_app(configured)
        async with serve(app) as server, ClientSession() as client:
            state = app[RUNTIME].algorithm.backends[0]
            estimate = state.estimate
            async with client.get(server.make_url("/work?request_id=1")) as response:
                assert response.status == expected
                assert response.headers["X-Backend-ID"] == "A"
                if expected == 500:
                    assert await response.json() == {"error": "test"}
            assert state.estimate == estimate
            assert state.available
            assert state.active == 0
            assert calls == [0, 1]
            assert app[RUNTIME].algorithm.assignments == 1


async def test_таймаут_включает_получение_тела(config):
    blocked = asyncio.Event()

    async def handler(request):
        if request.query["request_id"] == "0":
            return await reply(request)
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"partial":')
        await blocked.wait()
        return response

    async with stub_backend(config, handler) as (backend, _):
        app = create_app(
            replace(
                config, backends=(backend,), balancer=replace(config.balancer, request_timeout=0.03)
            )
        )
        async with serve(app) as server, ClientSession() as client:
            estimate = app[RUNTIME].algorithm.backends[0].estimate
            async with client.get(server.make_url("/work?request_id=1")) as response:
                assert response.status == 504
            assert app[RUNTIME].algorithm.backends[0].estimate == estimate


async def test_ошибка_подключения_502(config):
    async with stub_backend(config, reply) as (backend, backend_server):
        app = create_app(
            replace(
                config, backends=(backend,), balancer=replace(config.balancer, health_interval=100)
            )
        )
        async with serve(app) as server, ClientSession() as client:
            await backend_server.close()
            async with client.get(server.make_url("/work?request_id=1")) as response:
                assert response.status == 502
                assert response.headers["X-Backend-ID"] == "A"
            assert app[RUNTIME].algorithm.backends[0].active == 0


async def test_конкурентные_запросы_согласованы(config):
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal entered
        if request.query["request_id"] != "0":
            entered += 1
            if entered == 30:
                all_entered.set()
            await release.wait()
        return await reply(request)

    async with stub_backend(config, handler) as (backend, _):
        app = create_app(
            replace(
                config, backends=(backend,), balancer=replace(config.balancer, request_timeout=2)
            )
        )
        async with serve(app) as server, ClientSession() as client:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(client.get(server.make_url(f"/work?request_id={i}")))
                    for i in range(1, 31)
                ]
                try:
                    async with asyncio.timeout(1):
                        await all_entered.wait()
                    assert app[RUNTIME].algorithm.backends[0].active == 30
                finally:
                    release.set()
            numbers, exploration = [], []
            for task in tasks:
                async with task.result() as response:
                    assert response.status == 200
                    number = int(response.headers["X-Assignment-Number"])
                    numbers.append(number)
                    if response.headers["X-Selection-Mode"] == "exploration":
                        exploration.append(number)
            assert sorted(numbers) == list(range(1, 31))
            assert sorted(exploration) == [10, 20, 30]
            assert app[RUNTIME].algorithm.backends[0].active == 0


async def test_восстановление_после_старого_запроса_требует_повторной_пробы(config):
    entered, release = asyncio.Event(), asyncio.Event()
    available = True
    probe_ok = True
    probes = 0
    health_checks = 0

    async def handler(request):
        nonlocal probes
        if request.query["request_id"] == "0":
            probes += 1
            return web.Response(status=200 if probe_ok else 500)
        entered.set()
        await release.wait()
        return await reply(request)

    async def health(request):
        nonlocal health_checks
        health_checks += 1
        return web.Response(status=200 if available else 503)

    async with stub_backend(config, handler, health) as (backend, _):
        app = create_app(
            replace(
                config, backends=(backend,), balancer=replace(config.balancer, request_timeout=2)
            )
        )
        async with serve(app) as server, ClientSession() as client:
            state = app[RUNTIME].algorithm.backends[0]
            estimate = state.estimate
            async with asyncio.TaskGroup() as group:
                pending = group.create_task(client.get(server.make_url("/work?request_id=1")))
                try:
                    async with asyncio.timeout(1):
                        await entered.wait()
                    available = False
                    await eventually(lambda: not state.available and state.active == 1)
                    async with client.get(server.make_url("/work?request_id=2")) as response:
                        assert response.status == 503
                    available = True
                    probe_ok = False
                    checks_before_drain = health_checks
                    # Пока старый запрос не завершён, восстановительные проверки не идут.
                    await asyncio.sleep(0.04)
                    assert health_checks == checks_before_drain
                    assert state.successes == 0 and probes == 1
                finally:
                    release.set()
            async with pending.result() as response:
                assert response.status == 200
            assert state.estimate == estimate
            await eventually(lambda: probes >= 2)
            assert not state.available and state.active == 0
            probe_ok = True
            await eventually(lambda: state.available)
            assert probes >= 3
            assert state.active == 0


async def test_реальное_закрытие_и_повторный_запуск_бэкенда(config):
    backend = replace(config.backends[0], delay_ms=0)
    backend_server = TestServer(backend_app(backend))
    await backend_server.start_server()
    backend = replace(backend, port=backend_server.port)
    app = create_app(replace(config, backends=(backend,)))
    replacement = None
    try:
        async with serve(app) as server, ClientSession() as client:
            state = app[RUNTIME].algorithm.backends[0]
            await backend_server.close()
            await eventually(lambda: not state.available and state.active == 0)
            async with client.get(server.make_url("/work?request_id=1")) as response:
                assert response.status == 503
            replacement = TestServer(backend_app(backend), port=backend.port)
            await replacement.start_server()
            await eventually(lambda: state.available)
            async with client.get(server.make_url("/work?request_id=2")) as response:
                assert response.status == 200
    finally:
        await backend_server.close()
        if replacement:
            await replacement.close()


@pytest.mark.parametrize("finish_before_deadline", [True, False])
async def test_graceful_shutdown_завершает_или_отменяет_работу(config, finish_before_deadline):
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.query["request_id"] != "0":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await reply(request)

    async with stub_backend(config, handler) as (backend, _):
        app = create_app(
            replace(
                config, backends=(backend,), balancer=replace(config.balancer, request_timeout=2)
            )
        )
        server = TestServer(app)
        await server.start_server()
        runtime = app[RUNTIME]
        async with ClientSession() as client:

            async def send():
                try:
                    async with client.get(server.make_url("/work?request_id=1")) as response:
                        return response.status
                except ClientError:
                    return None

            pending = asyncio.create_task(send())
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                closing = asyncio.create_task(server.close())
                await eventually(lambda: runtime.stopping)
                if finish_before_deadline:
                    release.set()
                async with asyncio.timeout(1):
                    await closing
                    result = await pending
                assert result == (200 if finish_before_deadline else None)
                if not finish_before_deadline:
                    async with asyncio.timeout(1):
                        await cancelled.wait()
                assert runtime.session.closed
                assert not runtime.work_tasks
                assert runtime.algorithm.backends[0].active == 0
                assert all(task.done() for task in runtime.health_tasks)
            finally:
                release.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await server.close()


async def test_отключение_клиента_отменяет_исходящий_запрос(config):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    blocked = asyncio.Event()

    async def handler(request):
        if request.query["request_id"] != "0":
            entered.set()
            try:
                await blocked.wait()
            finally:
                cancelled.set()
        return await reply(request)

    async with stub_backend(config, handler) as (backend, _):
        app = create_app(
            replace(
                config, backends=(backend,), balancer=replace(config.balancer, request_timeout=2)
            )
        )
        async with serve(app) as server, ClientSession() as client:
            state = app[RUNTIME].algorithm.backends[0]
            estimate = state.estimate
            pending = asyncio.create_task(client.get(server.make_url("/work?request_id=1")))
            try:
                async with asyncio.timeout(1):
                    await entered.wait()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                async with asyncio.timeout(1):
                    await cancelled.wait()
                await eventually(lambda: state.active == 0)
                assert state.estimate == estimate
                assert not app[RUNTIME].work_tasks
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_замедление_и_исследование_восстановившего_скорость(balancer, monkeypatch):
    clock = 0
    delays = {"A": 0.5, "B": 0.1, "C": 0.15}

    async def forward(backend, request_id):
        nonlocal clock
        clock += delays[backend.config.id]
        return web.Response(status=200)

    monkeypatch.setattr(balancer_app, "perf_counter", lambda: clock)
    monkeypatch.setattr(balancer, "forward", forward)
    response = await balancer.handle_work(1)
    assert response.headers["X-Backend-ID"] == "A"
    assert balancer.algorithm.backends[0].estimate == pytest.approx(0.14)
    response = await balancer.handle_work(2)
    assert response.headers["X-Backend-ID"] == "B"

    delays["A"] = 0.05
    for request_id in range(3, 153):
        response = await balancer.handle_work(request_id)
    assert balancer.algorithm.backends[0].estimate < 0.1
    assert response.headers["X-Selection-Mode"] == "least_time"
    assert response.headers["X-Backend-ID"] == "A"
    assert all(backend.active == 0 for backend in balancer.algorithm.backends)


async def test_пороги_последовательных_проверок_и_повтор_неуспешной_пробы(balancer, monkeypatch):
    backend = balancer.algorithm.backends[0]
    # Результат проверки и ожидаемое состояние ПЕРЕД ней.
    checks = iter(
        [
            (False, True, 0, 0),
            (True, True, 1, 0),
            (False, True, 0, 0),
            (False, True, 1, 0),
            (True, False, 2, 0),
            (False, False, 2, 1),
            (True, False, 2, 0),
            (True, False, 2, 1),  # Первая проба вернёт 500.
            (True, False, 0, 0),
            (True, False, 0, 1),  # Новая пара успехов разрешит вторую пробу.
        ]
    )
    finished = asyncio.Event()
    blocked = asyncio.Event()

    async def health(backend):
        check = next(checks, None)
        if check is None:
            finished.set()
            await blocked.wait()
            return True
        success, available, failures, successes = check
        assert (backend.available, backend.failures, backend.successes) == (
            available,
            failures,
            successes,
        )
        assert backend.estimate == 0.05
        return success

    forward = AsyncMock(side_effect=[web.Response(status=500), web.Response(status=200)])
    clock = iter([0, 0.5, 1, 1.07])
    monkeypatch.setattr(balancer, "is_healthy", health)
    monkeypatch.setattr(balancer, "forward", forward)
    monkeypatch.setattr(balancer_app, "perf_counter", lambda: next(clock))
    # TaskGroup передаст в тест ошибку проверки, если фоновая задача упадёт.
    async with asyncio.TaskGroup() as group:
        task = group.create_task(balancer.check_health(backend))
        try:
            async with asyncio.timeout(1):
                await finished.wait()
            assert backend.available
            assert backend.failures == backend.successes == 0
            assert backend.estimate == pytest.approx(0.07)
            assert forward.await_count == 2
        finally:
            task.cancel()
