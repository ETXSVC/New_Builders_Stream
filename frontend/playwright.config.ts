import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // 60s test timeout (above Playwright's 30s default): this suite runs
  // against a `next dev` server, and the first navigation to each route
  // after a cold start pays Next.js's on-demand-compilation cost (confirmed
  // live: /auth/register and /auth/login both returned successfully well
  // within a second per the backend's own logs, but the client-side
  // navigation to the newly-compiled /account route still exceeded the
  // per-assertion default and failed the test). Left the per-assertion
  // `expect` timeout at its 5s default here — the two transitions that
  // actually pay the cold-compile cost (the /account redirects) carry
  // their own explicit `{ timeout: 15_000 }` override in the spec, so a
  // real latency regression anywhere else in the suite still fails fast.
  timeout: 60_000,
  use: {
    // The worktree's docker-compose.yml maps the frontend container to
    // host port 3001, not 3000 (this default) — running against Compose
    // requires E2E_BASE_URL=http://localhost:3001. This default suits
    // `npm run dev` outside Docker instead.
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
  },
});
