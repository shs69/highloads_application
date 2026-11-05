import math
from asyncio import TaskGroup
from collections import Counter
from dataclasses import asdict, dataclass
from time import perf_counter

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

from highloads_applications.configuration import integer, number
from highloads_applications.http_helpers import single_attempt


@dataclass
class RequestResult:
    request_id: int
    duration_sec: float = 0
    status: int | None = None
    backend_id: str | None = None
    balancer_request_id: int | None = None
    error: str | None = None


async def send_request(*, session: ClientSession, request_id: int, url: str):
    result = RequestResult(request_id=request_id)
    started_time = perf_counter()

    try:
        async with session.get(
            url, params={"request_id": request_id}, allow_redirects=False
        ) as response:
            result.status = response.status
            result.backend_id = response.headers.get("X-Backend-ID")

            if "X-Request-ID" in response.headers:
                result.balancer_request_id = int(response.headers["X-Request-ID"])

            # Считываем тело целиком, в том числе для ответов с ошибкой.
            _ = await response.read()
    except (ClientError, TimeoutError, ValueError) as error:
        result.error = type(error).__name__

    result.duration_sec = perf_counter() - started_time
    return result


async def parallel_load_mode(
    *, session: ClientSession, url: str, total_requests: int, concurrency: int
) -> list[RequestResult]:
    results = []

    next_request_id = 1

    async def worker():
        nonlocal next_request_id

        while next_request_id <= total_requests:
            request_id = next_request_id
            next_request_id += 1

            results.append(
                await send_request(session=session, request_id=request_id, url=url)
            )

    async with TaskGroup() as tg:
        for _ in range(min(concurrency, total_requests)):
            tg.create_task(worker())

    return sorted(results, key=lambda result: result.request_id)


async def sequential_mode(
    *, session: ClientSession, url: str, total_requests: int
) -> list[RequestResult]:
    results = []

    for request_id in range(1, total_requests + 1):
        results.append(
            await send_request(session=session, request_id=request_id, url=url)
        )

    return results


async def generate(
    *,
    mode: str,
    url: str = "http://127.0.0.1:8080/work",
    total_requests: int = 1000,
    concurrency: int = 100,
    timeout_sec: float = 3,
) -> dict[str, object]:
    _ = integer(total_requests, "requests")
    _ = integer(concurrency, "concurrency")
    _ = number(timeout_sec, "timeout")

    if mode not in {"sequential", "parallel"}:
        raise ValueError("Неизвестный режим генератора")

    timeout = ClientTimeout(total=timeout_sec, ceil_threshold=math.inf)
    started_time = perf_counter()
    results = []

    async with ClientSession(
        timeout=timeout,
        connector=TCPConnector(limit=concurrency),
        middlewares=(single_attempt,),
    ) as session:
        match mode:
            case "sequential":
                results = await sequential_mode(
                    session=session, url=url, total_requests=total_requests
                )
            case "parallel":
                results = await parallel_load_mode(
                    session=session,
                    url=url,
                    total_requests=total_requests,
                    concurrency=concurrency,
                )

    duration_sec = perf_counter() - started_time
    successful = [
        result for result in results if result.status == 200 and result.error is None
    ]

    assignments = Counter(
        result.backend_id for result in results if result.backend_id is not None
    )
    successful_by_backend = Counter(
        result.backend_id for result in successful if result.backend_id is not None
    )

    return {
        "parameters": {
            "mode": mode,
            "url": url,
            "requests": total_requests,
            "concurrency": concurrency if mode == "parallel" else 1,
            "timeout_sec": timeout_sec,
        },
        "results": [asdict(result) for result in results],
        "duration_sec": duration_sec,
        "assignments_by_backend": dict(assignments),
        "successful_by_backend": dict(successful_by_backend),
        "success_requests_count": len(successful),
    }
