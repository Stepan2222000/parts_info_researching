"""Entry point воркера Этапа 2:

    python -m parts_research.cli.worker

Запускает run_forever() с graceful-shutdown на SIGINT/SIGTERM (drain inflight).
Можно поднимать несколько процессов на одну общую очередь."""

from __future__ import annotations

import asyncio

from ..queue.worker import run_forever


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
