"""Entry point HTTP-API (Этап 4):

    python -m parts_research.cli.api            # 0.0.0.0:8000
    PARTS_RESEARCH_API_HOST/PORT для переопределения

Поднимает FastAPI-приложение (uvicorn) с эндпоинтами submit/queue/run/curator.
Один процесс на всю систему; worker и curator-REPL остаются отдельными."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PARTS_RESEARCH_API_HOST", "0.0.0.0")
    port = int(os.environ.get("PARTS_RESEARCH_API_PORT", "8000"))
    uvicorn.run("parts_research.api.app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
