import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // 60s test timeout / 15s per-assertion timeout (both above Playwright's
  // defaults of 30s / 5s): this suite runs against a `next dev` server, and
  // the first navigation to each route after a cold start pays Next.js's
  // on-demand-compilation cost (confirmed live: /auth/register and
  // /auth/login both returned successfully well within a second per the
  // backend's own logs, but the client-side navigation to the
  // newly-compiled /account route still exceeded the 5s default and failed
  // the test). A production build wouldn't pay this cost, but this suite
  // intentionally exercises the same dev server developers run locally.
  timeout: 60_000,
  expect: {
    timeout: 15_000,
  },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
  },
});
