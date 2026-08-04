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
  // (Tracing is set in `use` below, and no longer keyed to the retry — see
  // the note there for why the retry was the wrong attempt to record.)
  retries: 1,
  // ...and CI must ACT on that signal, not merely print it.
  //
  // `retries: 1` above claims a flaky test "still shows as flaky, which is
  // the signal worth having". It did show — in the log, where nobody looks.
  // Playwright exits 0 when a retry passes, so the GitHub check went green
  // and `2 flaky, 6 passed` was indistinguishable from a clean run at the
  // only level anyone actually reads: the check-run conclusion.
  //
  // That blind spot hid two real product bugs, both since fixed (#47, #48):
  // a superseded fetch overwriting fresh state, and the API returning 201
  // for a write that was not yet readable. Neither was test flakiness.
  // Both were found only by opening raw job logs by hand — and neither
  // would have needed finding had the job gone red the first time it
  // flaked.
  //
  // CI-only (Playwright's own documented pattern) so local iteration stays
  // usable: someone re-running one spec does not want the whole run failed
  // by a retry-passed test, but a merge gate absolutely does. GitHub
  // Actions sets CI=true for every step; set CI=1 locally to reproduce the
  // gate's behaviour.
  failOnFlakyTests: !!process.env.CI,
  use: {
    // `retain-on-first-failure`, NOT `on-first-retry`, and the two are almost
    // opposites in practice. `on-first-retry` traces the RETRY — so when a
    // test fails once and passes on retry, the artifact you get is a
    // recording of the run that worked. The failure, which is the only thing
    // anyone opens a trace for, is the one attempt not recorded.
    //
    // That is not hypothetical: the estimate-duplicate flake was diagnosed
    // from a Playwright aria snapshot in `error-context.md` because the
    // trace in the artifact was of the passing retry, and the network log
    // that would have said which request failed did not exist. Two CI runs
    // and a manual code trace substituted for one artifact.
    //
    // `failOnFlakyTests` below is what makes this the right trade. A flake is
    // already a red gate here, so the first attempt failing IS the event
    // worth recording. `retain-on-first-failure` records every first attempt
    // and keeps the recording only when it failed — so a green run still
    // uploads nothing, which was the original comment's actual concern.
    trace: "retain-on-first-failure",
    // The worktree's docker-compose.yml maps the frontend container to
    // host port 3001, not 3000 (this default) — running against Compose
    // requires E2E_BASE_URL=http://localhost:3001. This default suits
    // `npm run dev` outside Docker instead.
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
  },
});
