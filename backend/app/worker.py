"""Cloud collector worker — deploy separately from the API (RUN_MODE=worker)."""

from __future__ import annotations

import asyncio
import logging
import signal

from app.collector.scheduler import start_scheduler, stop_scheduler
from app.config import settings
from app.database import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pgwatch.worker")


async def _run() -> None:
    if settings.run_mode not in ("worker", "all"):
        logger.warning("RUN_MODE=%s; worker process expects worker or all", settings.run_mode)
    await init_db()
    start_scheduler()
    logger.info("pgwatch worker running (interval=%ss)", settings.collect_interval_seconds)
    stop = asyncio.Event()

    def _handle_sig(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_sig)

    await stop.wait()
    stop_scheduler()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
