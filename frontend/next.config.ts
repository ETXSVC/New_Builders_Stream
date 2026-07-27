import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";

// Security headers the APP owns (they describe its own asset origins and
// apply in dev too). HSTS deliberately lives at the reverse proxy only
// (deploy/Caddyfile) — it is meaningless without TLS and harmful if the
// app emitted it over plain HTTP in development.
//
// CSP: 'unsafe-inline' for scripts/styles is the honest cost of Next's
// inline bootstrap without a nonce pipeline; a nonce-based strict CSP is
// a documented follow-up (docs/11-production-deployment.md), not blocking.
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
  {
    key: "Content-Security-Policy",
    value:
      "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; " +
      "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'",
  },
];

const nextConfig: NextConfig = {
  poweredByHeader: false,
  // standalone output is what the production Docker image runs
  // (node server.js); env-gated because `next start` — used by e2e-ci and
  // local `npm run start` — does not serve a standalone build.
  output: process.env.BUILD_STANDALONE ? "standalone" : undefined,
  async headers() {
    return [{ source: "/(.*)", headers: securityHeaders }];
  },
};

// Sentry wrapping is applied ONLY when a DSN is configured. Unwrapped
// otherwise, so a build with no Sentry configuration produces exactly the
// bytes it produced before this integration existed — which is what keeps
// `npm run build` in CI, and every local build, unaffected.
const sentryEnabled = Boolean(
  process.env.NEXT_PUBLIC_SENTRY_DSN || process.env.SENTRY_DSN
);

export default sentryEnabled
  ? withSentryConfig(nextConfig, {
      silent: true,
      // Route browser events through THIS origin instead of
      // ingest.sentry.io. Two reasons, in order of importance:
      //
      // 1. The CSP above pins `connect-src 'self'`. A direct-to-Sentry
      //    POST would be blocked by it, and the alternative — adding the
      //    ingest host to connect-src — widens the policy for every page
      //    to benefit telemetry. Tunnelling keeps the policy as strict as
      //    it is today, which is the point of having written it.
      // 2. Ad blockers routinely block requests to known telemetry hosts,
      //    so a tunnelled path also reports the errors of the users most
      //    likely to be running one.
      //
      // Cost: error events traverse the Next server, so a total frontend
      // outage reports nothing. Server-side errors still report directly
      // via instrumentation.ts, so the outage itself is not silent.
      tunnelRoute: "/monitoring",
      // Source-map upload needs SENTRY_AUTH_TOKEN/org/project. Without
      // them the build must still succeed — a missing telemetry credential
      // is not a reason to fail a deploy — so uploads are skipped rather
      // than attempted and errored.
      sourcemaps: { disable: !process.env.SENTRY_AUTH_TOKEN },
      // Keeps the uploaded maps out of the served bundle.
      widenClientFileUpload: false,
      disableLogger: true,
    })
  : nextConfig;
