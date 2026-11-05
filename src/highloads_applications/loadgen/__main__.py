import argparse
import asyncio
import json
from pathlib import Path

from highloads_applications.configuration import integer, number
from highloads_applications.loadgen.loadgen import generate


def main() -> None:
    parser = argparse.ArgumentParser(description="Генератор запросов к балансировщику")
    _ = parser.add_argument(
        "--mode", choices=("sequential", "parallel"), default="parallel"
    )
    _ = parser.add_argument("--url", default="http://127.0.0.1:8080/work")
    _ = parser.add_argument("--requests", type=int, default=1000)
    _ = parser.add_argument("--concurrency", type=int, default=100)
    _ = parser.add_argument("--timeout", type=float, default=3)
    _ = parser.add_argument(
        "--output", type=Path, help="Файл для сохранения результатов"
    )
    args = parser.parse_args()

    try:
        _ = integer(args.requests, "requests")
        _ = integer(args.concurrency, "concurrency")
        _ = number(args.timeout, "timeout")
    except ValueError as error:
        parser.error(str(error))

    report = asyncio.run(
        generate(
            mode=args.mode,
            url=args.url,
            total_requests=args.requests,
            concurrency=args.concurrency,
            timeout_sec=args.timeout,
        )
    )

    text = json.dumps(report, ensure_ascii=False, indent=2)

    if args.output is None:
        print(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _ = args.output.write_text(text + "\n", encoding="utf-8")

        print(f"Результаты сохранены: {args.output}")


if __name__ == "__main__":
    main()
