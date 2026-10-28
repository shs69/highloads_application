import asyncio
import logging
import math
from collections.abc import AsyncIterator
from time import perf_counter

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from highloads_applications.configuration import SystemConfig
from highloads_applications.http_helpers import request_id as parse_request_id
from highloads_applications.http_helpers import single_attempt

from .algorithms import BackendState, LeastTime

logger = logging.getLogger(__name__)


class Balancer:
    def __init__(self, config: SystemConfig, session: ClientSession) -> None:
        self.settings = config.balancer
        self.algorithm = LeastTime(
            config.backends,
            exploration_interval=config.balancer.exploration_interval,
            alpha=config.balancer.alpha,
        )
        self.session = session
        self.health_tasks: list[asyncio.Task[None]] = []
        self.work_tasks: set[asyncio.Task[web.StreamResponse]] = set()
        self.stopping = False
        self.work_timeout = ClientTimeout(
            total=config.balancer.request_timeout,
            ceil_threshold=math.inf,
        )
        self.health_timeout = ClientTimeout(
            total=config.balancer.health_timeout,
            ceil_threshold=math.inf,
        )

    async def handle_work(self, request_id: int) -> web.Response:
        if self.stopping:
            raise web.HTTPServiceUnavailable(text="Balancer shutting down")
        backend = self.algorithm.select_backend()
        if backend is None:
            raise web.HTTPServiceUnavailable(text="No healthy backends")

        # Сохраняем данные назначения до await: другие запросы изменят общий счётчик.
        number = self.algorithm.assignments
        estimate_at_assignment = backend.estimate
        mode = "least_time"
        if number % self.settings.exploration_interval == 0:
            mode = "exploration"
        backend.active += 1
        task = asyncio.current_task()
        assert task is not None
        self.work_tasks.add(task)
        started = perf_counter()
        status = None
        logger.info(
            f"assignment number={number} request_id={request_id} "
            f"backend={backend.config.id} mode={mode}"
        )
        try:
            response = await self.forward(backend, request_id)
            status = response.status
        finally:
            duration = perf_counter() - started
            estimate_before_completion = backend.estimate
            backend.active -= 1
            self.algorithm.record_response(backend, duration, successful=status == 200)
            self.work_tasks.discard(task)
            result = status if status is not None else "cancelled"
            logger.info(
                f"completion number={number} request_id={request_id} "
                f"backend={backend.config.id} result={result} duration={duration:.6f} "
                f"ewma_before={estimate_before_completion} ewma_after={backend.estimate}"
            )

        response.headers.update(
            {
                "X-Backend-ID": backend.config.id,
                "X-Assignment-Number": str(number),
                "X-Selection-Mode": mode,
                "X-EWMA-Before": str(estimate_at_assignment),
                "X-EWMA-After": str(backend.estimate),
            }
        )
        return response

    async def forward(self, backend: BackendState, request_id: int) -> web.Response:
        try:
            async with self.session.get(
                backend.config.url + "/work",
                params={"request_id": request_id},
                timeout=self.work_timeout,
                allow_redirects=False,
            ) as response:
                body = await response.read()
                content_type = response.headers.get("Content-Type", "application/json")
                return web.Response(
                    status=response.status,
                    body=body,
                    headers={"Content-Type": content_type},
                )
        except TimeoutError:
            return web.json_response({"error": "Backend timeout"}, status=504)
        except ClientError as error:
            logger.warning(f"connection_error backend={backend.config.id} error={error}")
            return web.json_response({"error": "Backend connection error"}, status=502)

    async def probe(self, backend: BackendState) -> None:
        logger.info(f"probe_start backend={backend.config.id} request_id=0")
        started = perf_counter()
        response = await self.forward(backend, 0)
        duration = perf_counter() - started
        backend.failures = 0
        backend.successes = 0
        backend.available = response.status == 200
        if backend.available:
            backend.estimate = duration
        logger.info(
            f"probe_end backend={backend.config.id} result={response.status} "
            f"estimate={backend.estimate}"
        )

    async def is_healthy(self, backend: BackendState) -> bool:
        try:
            async with self.session.get(
                backend.config.url + "/health",
                timeout=self.health_timeout,
                allow_redirects=False,
            ) as response:
                await response.read()
                return response.status == 200
        except (ClientError, TimeoutError):
            return False

    async def check_health(self, backend: BackendState) -> None:
        while True:
            await asyncio.sleep(self.settings.health_interval)
            # Восстановительные проверки начинаются только после старых запросов.
            if not backend.available and backend.active > 0:
                continue

            success = await self.is_healthy(backend)
            if not success:
                logger.warning(f"health_failed backend={backend.config.id}")

            if backend.available:
                if success:
                    backend.failures = 0
                else:
                    backend.failures += 1
                if backend.failures >= self.settings.failure_threshold:
                    backend.available = False
                    backend.successes = 0
                    logger.info(f"backend_down backend={backend.config.id} active={backend.active}")
                continue

            if not success:
                backend.successes = 0
                continue

            backend.successes += 1
            if backend.successes >= self.settings.recovery_threshold:
                await self.probe(backend)
                if backend.available:
                    logger.info(f"backend_recovered backend={backend.config.id}")

    async def drain(self) -> None:
        self.stopping = True
        logger.info(f"shutdown active={len(self.work_tasks)}")
        if not self.work_tasks:
            return
        _, pending = await asyncio.wait(
            self.work_tasks,
            timeout=self.settings.shutdown_timeout,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


CONFIG = web.AppKey("config", SystemConfig)
RUNTIME = web.AppKey("balancer", Balancer)


async def work(request: web.Request) -> web.Response:
    balancer = request.app[RUNTIME]
    return await balancer.handle_work(parse_request_id(request))


async def shutdown(app: web.Application) -> None:
    await app[RUNTIME].drain()


async def lifecycle(app: web.Application) -> AsyncIterator[None]:
    # Сессия общая, но force_close запрещает повторное использование TCP-соединений.
    async with ClientSession(
        connector=TCPConnector(force_close=True, limit=0),
        middlewares=(single_attempt,),
    ) as session:
        balancer = Balancer(app[CONFIG], session)
        app[RUNTIME] = balancer
        logger.info("balancer_start")
        try:
            for backend in balancer.algorithm.backends:
                await balancer.probe(backend)

            for backend in balancer.algorithm.backends:
                task = asyncio.create_task(
                    balancer.check_health(backend),
                    name=f"health-{backend.config.id}",
                )
                balancer.health_tasks.append(task)
            # До yield выполняется запуск; после него — освобождение ресурсов при остановке.
            yield
        finally:
            for task in balancer.health_tasks:
                task.cancel()
            await asyncio.gather(*balancer.health_tasks, return_exceptions=True)
    logger.info("balancer_stop")


def create_app(config: SystemConfig) -> web.Application:
    app = web.Application()
    app[CONFIG] = config
    app.router.add_get("/work", work)
    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    return app
