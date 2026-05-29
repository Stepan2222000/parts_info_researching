"""Entry point куратора (Этап 3):

    python -m parts_research.cli.curator              # интерактивный REPL
    python -m parts_research.cli.curator --message "обработай очередь"   # one-shot

REPL — основной режим (этап 3 до UI). --message удобно для прогонов и автоматизации."""

from __future__ import annotations

import asyncio
import sys

from ..curator.repl import run_once, run_repl


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--message":
        if len(args) < 2:
            print('usage: python -m parts_research.cli.curator --message "TEXT"', file=sys.stderr)
            raise SystemExit(2)
        asyncio.run(run_once(args[1]))
    else:
        asyncio.run(run_repl())


if __name__ == "__main__":
    main()
