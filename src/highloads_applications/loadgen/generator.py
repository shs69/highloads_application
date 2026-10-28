import asyncio
import logging
import math
from collections import Counter
from dataclasses import asdict, dataclass
from statistics import mean
from time import perf_counter

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

from highloads_applications.configuration import LoadgenConfig
from highloads_applications.http_helpers import single_attempt

logger = logging.getLogger(__name__)


@dataclass
class RequestResult:
    request_id: int
    duration: float = 0
    status: int | None = None
    backend_id: str | None = None
    assignment_number: int | None = None
    selection_mode: str | None = None
    ewma_before: float | None = None
    ewma_after: float | None = None
    error: str | None = None


async def send_request(session: ClientSession, url: str, request_id: int) -> RequestResult:
    result = RequestResult(request_id=request_id)
    started = perf_counter()
    try:
        async with session.get(
            url,
            params={"request_id": request_id},
            allow_redirects=False,
        ) as response:
            result.status = response.status
            result.backend_id = response.headers.get("X-Backend-ID")
            if result.backend_id is not None:
                result.assignment_number = int(response.headers["X-Assignment-Number"])
                result.selection_mode = response.headers["X-Selection-Mode"]
                result.ewma_before = float(response.headers["X-EWMA-Before"])
                result.ewma_after = float(response.headers["X-EWMA-After"])
            await response.read()
    except (ClientError, TimeoutError, KeyError, ValueError) as error:
        result.error = type(error).__name__
    result.duration = perf_counter() - started
    return result


def percentile(sorted_values: list[float], fraction: float) -> float:
    position = (len(sorted_values) - 1) * fraction
    lower = sorted_values[math.floor(position)]
    upper = sorted_values[math.ceil(position)]
    return lower + (upper - lower) * (position % 1)


def latency_statistics(results: list[RequestResult]) -> dict[str, float | None]:
    times = sorted(result.duration for result in results)
    return {
        "mean": mean(times),
        "min": times[0],
        "max": times[-1],
        "p50": percentile(times, 0.5) if len(times) >= 2 else None,
        "p95": percentile(times, 0.95) if len(times) >= 20 else None,
        "p99": percentile(times, 0.99) if len(times) >= 100 else None,
    }


def summarize(results: list[RequestResult], duration: float) -> dict[str, object]:
    successful = [result for result in results if result.status == 200 and result.error is None]
    assigned = [result for result in results if result.backend_id is not None]
    http_errors = [str(result.status) for result in results if result.status not in (None, 200)]
    client_errors = [result.error for result in results if result.error is not None]
    successful_backends = [
        result.backend_id for result in successful if result.backend_id is not None
    ]

    return {
        "sent": len(results),
        "successful": len(successful),
        "http_errors": dict(Counter(http_errors)),
        "client_errors": dict(Counter(client_errors)),
        "assignments_by_backend": dict(Counter(result.backend_id for result in assigned)),
        "successful_by_backend": dict(Counter(successful_backends)),
        "without_assignment_metadata": len(results) - len(assigned),
        "latency_seconds": latency_statistics(results),
        "duration_seconds": duration,
        "requests_per_second": len(results) / duration,
    }


async def generate(config: LoadgenConfig) -> dict[str, object]:
    url = config.url.rstrip("/") + "/work"
    timeout = ClientTimeout(total=config.timeout, ceil_threshold=math.inf)
    started = perf_counter()
    logger.info(f"load_start requests={config.requests} mode={config.mode}")

    async with ClientSession(
        timeout=timeout,
        connector=TCPConnector(limit=config.concurrency),
        middlewares=(single_attempt,),
    ) as session:
        if config.mode == "sequential":
            results = []
            for request_id in range(1, config.requests + 1):
                result = await send_request(session, url, request_id)
                results.append(result)
        else:
            semaphore = asyncio.Semaphore(config.concurrency)

            async def send_limited(request_id: int) -> RequestResult:
                # Ожидание свободного места не входит во время HTTP-запроса.
                async with semaphore:
                    return await send_request(session, url, request_id)

            tasks = []
            # Группа дожидается всех запросов, а при сбое отменяет оставшиеся.
            async with asyncio.TaskGroup() as group:
                for request_id in range(1, config.requests + 1):
                    tasks.append(group.create_task(send_limited(request_id)))
            results = [task.result() for task in tasks]

    duration = perf_counter() - started
    logger.info(f"load_stop duration={duration:.6f}")
    return {
        "parameters": asdict(config),
        "summary": summarize(results, duration),
        "requests": [asdict(result) for result in results],
    }
