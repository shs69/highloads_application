import logging

from aiohttp import (
    ClientConnectionError,
    ClientHandlerType,
    ClientOSError,
    ClientRequest,
    ClientResponse,
    ServerDisconnectedError,
    web,
)


async def single_attempt(request: ClientRequest, handler: ClientHandlerType) -> ClientResponse:
    # aiohttp повторяет GET при этих ошибках даже с force_close=True.
    # ClientConnectionError позволяет вернуть 502 без повторной отправки.
    try:
        return await handler(request)
    except (ClientOSError, ServerDisconnectedError) as error:
        raise ClientConnectionError(str(error)) from error


def request_id(request: web.Request, *, allow_probe: bool = False) -> int:
    values = request.query.getall("request_id", [])

    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
        raise web.HTTPBadRequest(text="request_id must be a single nonnegative integer")

    try:
        value = int(values[0])
    except ValueError as error:
        raise web.HTTPBadRequest(text="request_id is too large") from error

    if value == 0 and not allow_probe:
        raise web.HTTPBadRequest(text="request_id=0 is reserved for internal probes")
    return value


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
