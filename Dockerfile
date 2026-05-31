# ============================================================
# parts_research — Python image (worker / curator / api).
# One image, three commands (см. docker-compose.yml).
# Base: python:3.13-slim; pip install --no-cache-dir (без lock-файлов).
# ============================================================
FROM python:3.13-slim

WORKDIR /app

# Зависимости ставим первыми — слой кэшируется, пока pyproject не менялся.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Правила/спека читаются по пути parents[3] == /app (prompts.py, curator/agent_factory.py).
COPY research_rules.md curator_rules.md save_to_smart.md ./

ENV PYTHONUNBUFFERED=1

# По умолчанию — воркер; compose переопределяет команду для curator/api.
CMD ["python", "-m", "parts_research.cli.worker"]
