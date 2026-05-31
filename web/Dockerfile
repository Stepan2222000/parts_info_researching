# ============================================================
# parts_research_app — Next.js 16 (standalone) image.
# Build с pnpm (npm i -g pnpm), runner — минимальный standalone-сервер.
# ============================================================
FROM node:26-slim AS builder
WORKDIR /app
RUN npm install -g pnpm@11.2.2
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM node:26-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
# PARTS_RESEARCH_API_URL подставляется compose'ом (http://api:8000).
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
