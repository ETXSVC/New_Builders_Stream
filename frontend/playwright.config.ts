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
  // One retry, and a trace kept only from it.
  //
  // Without these a flake is a bare red X: the run is gone, and the only
  // way to learn anything is to re-run and hope it reproduces. This suite
  // has flaked exactly once (an assertion that matched a controlled
  // <textarea> before the POST returned), and diagnosing it took a local
  // stack because there was no artifact.
  //
  // `retries: 1` is deliberately not higher. The point is to produce a
  // trace and distinguish "flaky" from "broken" in the report, NOT to
  // paper over instability — a test that passes on retry still shows as
  // flaky, which is the signal worth having. A real regression fails both
  // attempts and stays red.
  //
  // `on-first-retry` rather than `on`: tracing every attempt of a green
  // run costs time and uploads artifacts nobody opens.
  retries: 1,
  use: {
    trace: "on-first-retry",
    // The worktree's docker-compose.yml maps the frontend container to
    // host port 3001, not 3000 (this default) — running against Compose
    // requires E2E_BASE_URL=http://localhost:3001. This default suits
    // `npm run dev` outside Docker instead.
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
  },
});
