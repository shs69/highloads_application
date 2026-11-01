import asyncio
import time


async def say_and_sleep(delay: float, text: str):
    await asyncio.sleep(delay)
    print(text)


async def main():
    task1 = asyncio.create_task(say_and_sleep(1, "hello"))

    task2 = asyncio.create_task(say_and_sleep(1, "world"))

    print(f"started at {time.strftime('%X')}")

    # Wait until both tasks are completed (should take
    # around 2 seconds.)
    await task1
    await task2

    print(f"finished at {time.strftime('%X')}")


asyncio.run(main())
