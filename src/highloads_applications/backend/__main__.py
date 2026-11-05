import argparse
import logging
from pathlib import Path

from aiohttp import web

from highloads_applications.configuration import (
    DEFAULT_CONFIG,
    ConfigurationError,
    load_config,
)

from .server import create_server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="HTTP-сервер")
    _ = parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="Конфигурация для сервера"
    )
    _ = parser.add_argument("--id", required=True, help="Идентификатор сервера")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        backend = None

        for back in config.backends:
            if back.id == args.id:
                backend = back
                break

        if backend is None:
            raise ConfigurationError(f"Неизвестный ID бекенда: {args.id}")
    except ConfigurationError as error:
        parser.error(str(error))

    web.run_app(
        create_server(backend=backend),
        host=backend.host,
        port=backend.port,
        handler_cancellation=True,
    )


if __name__ == "__main__":
    main()
