"""Entry point публичного HTTP-API для внешних систем:

    python -m parts_research.cli.public_api          # 0.0.0.0:8100
    PARTS_RESEARCH_PUBLIC_API_HOST / PARTS_RESEARCH_PUBLIC_API_PORT для переопределения

Поднимает ТОЛЬКО ресерч-эндпоинты (submit-and-wait + result + health), без
куратора и UI-выборок. Это приложение публикуется наружу; полный `cli.api`
(с куратором и UI) остаётся внутренним."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("PARTS_RESEARCH_PUBLIC_API_HOST", "0.0.0.0")
    port = int(os.environ.get("PARTS_RESEARCH_PUBLIC_API_PORT", "8100"))
    uvicorn.run("parts_research.api.public_app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
