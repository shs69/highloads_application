import argparse
from pathlib import Path

from aiohttp import web

from highloads_applications.configuration import DEFAULT_CONFIG, ConfigurationError, load_config
from highloads_applications.http_helpers import configure_logging

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовый HTTP-бэкенд")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--id", required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        selected = next((item for item in config.backends if item.id == args.id), None)
        if selected is None:
            raise ConfigurationError(f"Неизвестный backend ID: {args.id}")
    except ConfigurationError as error:
        parser.error(str(error))
    configure_logging()
    web.run_app(
        create_app(selected),
        host=selected.host,
        port=selected.port,
        shutdown_timeout=config.balancer.shutdown_timeout,
        handler_cancellation=True,
        access_log=None,
    )


if __name__ == "__main__":
    main()
