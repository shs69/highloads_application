import asyncio
import json

from highloads_applications.loadgen.loadgen import generate


def main():
    report = asyncio.run(generate(mode="parallel"))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
