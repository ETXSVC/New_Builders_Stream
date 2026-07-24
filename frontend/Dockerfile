# Multi-stage: `production` (default, last stage) runs the standalone
# next build; the `dev` target preserves the previous dev-image behavior
# for docker-compose.yml (build.target: dev + source bind-mount).

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS dev
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]

FROM node:22-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# BUILD_STANDALONE gates `output: "standalone"` in next.config.ts — only
# this image consumes the standalone server; `next start` (local, e2e CI)
# does not serve standalone builds.
ENV BUILD_STANDALONE=1 NODE_ENV=production
RUN npm run build

FROM node:22-slim AS production
WORKDIR /app
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
