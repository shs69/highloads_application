import argparse
import logging
from pathlib import Path

from aiohttp import web

from highloads_applications.configuration import (
    DEFAULT_CONFIG,
    ConfigurationError,
    load_config,
)

from .balancer import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP-балансировщик Least Time")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigurationError as error:
        parser.error(str(error))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    web.run_app(
        create_server(config=config),
        host=config.balancer.host,
        port=config.balancer.port,
        shutdown_timeout=config.balancer.shutdown_timeout,
        handler_cancellation=True,
        access_log=None,
    )


if __name__ == "__main__":
    main()
