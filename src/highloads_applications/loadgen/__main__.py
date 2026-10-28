import argparse
import asyncio
import json
import logging
import signal
from dataclasses import replace
from pathlib import Path

from highloads_applications.configuration import (
    DEFAULT_CONFIG,
    ConfigurationError,
    LoadgenConfig,
    load_config,
)
from highloads_applications.http_helpers import configure_logging

from .generator import generate


async def run_until_signal(config: LoadgenConfig) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await generate(config)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def read_options() -> LoadgenConfig:
    parser = argparse.ArgumentParser(description="Генератор HTTP-нагрузки")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--url")
    parser.add_argument("--requests", type=int)
    parser.add_argument("--mode", choices=["sequential", "concurrent"])
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--output")
    options = vars(parser.parse_args())
    try:
        config = load_config(options.pop("config")).loadgen
        overrides = {}
        for name, value in options.items():
            if value is not None:
                overrides[name] = value
        return replace(config, **overrides)
    except ConfigurationError as error:
        parser.error(str(error))


def main() -> None:
    config = read_options()
    configure_logging()
    try:
        report = asyncio.run(run_until_signal(config))
        output = Path(config.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
        output.write_text(content + "\n")
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("load_cancelled")
        return
    except OSError as error:
        raise SystemExit(f"Не удалось сохранить отчёт: {error}") from error
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    print(f"Результаты: {output}")


if __name__ == "__main__":
    main()
