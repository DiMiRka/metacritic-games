import asyncio
import logging

log = logging.getLogger("worker")


async def main() -> None:
    log.info("worker started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
