import math
from asyncio import TaskGroup
from dataclasses import asdict, dataclass
from time import perf_counter

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector


@dataclass
class RequestResult:
    request_id: int
    duration_sec: float = 0
    status: int | None = None
    backend_id: str | None = None
    error: str | None = None


async def send_request(*, session: ClientSession, request_id: int, url: str):
    result = RequestResult(request_id=request_id)
    started_time = perf_counter()

    try:
        async with session.get(
            url, params={"request_id": request_id}, allow_redirects=False
        ) as response:
            result.status = response.status

            response_json_data = await response.json()
            # Надо будет изменить способ получения айди бекенда на заголовки от балансера
            result.backend_id = response_json_data["backend_id"]
    except (ClientError, TimeoutError, KeyError, ValueError) as error:
        result.error = type(error).__name__

    result.duration_sec = perf_counter() - started_time
    return result


async def parallel_load_mode(
    *, session: ClientSession, url: str, total_requests: int
) -> list[RequestResult]:
    results = []

    next_request_id = 0

    async def worker():
        nonlocal next_request_id

        while next_request_id < total_requests:
            request_id = next_request_id
            next_request_id += 1

            results.append(
                await send_request(session=session, request_id=request_id, url=url)
            )

    async with TaskGroup() as tg:
        for _ in range(100):
            tg.create_task(worker())

    return results


async def sequantial_mode(
    *, session: ClientSession, url: str, total_requests: int
) -> list[RequestResult]:
    results = []

    for request_id in range(total_requests):
        results.append(
            await send_request(session=session, request_id=request_id, url=url)
        )

    return results


async def generate(*, mode: str) -> dict[str, object]:
    url = "http://127.0.0.1:9001/work"
    timeout = ClientTimeout(total=2, ceil_threshold=math.inf)
    started_time = perf_counter()
    results = []

    async with ClientSession(
        timeout=timeout, connector=TCPConnector(limit=100)
    ) as session:
        match mode:
            case "sequential":
                results = await sequantial_mode(
                    session=session, url=url, total_requests=1000
                )
            case "parallel":
                results = await parallel_load_mode(
                    session=session, url=url, total_requests=1000
                )

    duration_sec = perf_counter() - started_time
    return {
        "results": [asdict(result) for result in results],
        "duration_sec": str(duration_sec),
        "success_requests_count": sum(
            request.status == 200 and request.error is None for request in results
        ),
    }
