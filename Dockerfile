FROM node:22-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN npm ci

COPY tsconfig.json codex_rules.md curator_rules.md ./
COPY migrations ./migrations
COPY src ./src

# Команда задаётся в docker-compose (worker / exa-proxy / curator).
CMD ["node", "-e", "console.error('set command via compose'); process.exit(1)"]
