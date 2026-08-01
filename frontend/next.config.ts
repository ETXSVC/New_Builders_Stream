import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";

// Security headers the APP owns (they describe its own asset origins and
// apply in dev too). HSTS deliberately lives at the reverse proxy only
// (deploy/Caddyfile) — it is meaningless without TLS and harmful if the
// app emitted it over plain HTTP in development.
//
// THE CSP IS NOT HERE ANY MORE. It is built per request in middleware.ts,
// because a nonce cannot be a static string: the whole point is that it
// differs every response. Everything below is genuinely constant, so it
// stays where a constant belongs.
const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

// Hostnames, besides localhost, that `next dev` should treat as its own
// origin. Comma-separated, from the environment rather than hardcoded,
// because the right value is whatever the developer's box is called and
// baking one machine's name into a committed file helps exactly one person.
//
// What this fixes: Next's dev server serves its INTERNAL endpoints —
// `/__nextjs_font/*`, `/_next/webpack-hmr` — only to origins it trusts, and
// out of the box that means localhost. Browsing the dev stack by hostname
// (http://solaris:3001) therefore gives a 403 on every font and a
// webpack-hmr WebSocket that reconnects forever, with the app itself working
// fine — which reads like a broken build and is not one.
//
// Dev only, by construction: `allowedDevOrigins` is not consulted by
// `next build`/`next start`, so this cannot widen anything in production or
// in CI. It is NOT a CSP relaxation and does not touch `securityHeaders`
// above — app routes were always reachable from these origins; only Next's
// own dev machinery was not.
// Next matches these against the request Origin's HOSTNAME, so "solaris:3001"
// or "http://solaris:3001" silently never match and the 403s continue with
// the config looking correct. Both forms are the obvious things to write —
// the browser reports the failing origin with its port, and that is what you
// copy — so strip scheme and port here rather than document a trap.
const allowedDevOrigins = (process.env.DEV_ALLOWED_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim().replace(/^[a-z]+:\/\//i, "").replace(/:\d+$/, ""))
  .filter(Boolean);

const nextConfig: NextConfig = {
  poweredByHeader: false,
  ...(allowedDevOrigins.length > 0 ? { allowedDevOrigins } : {}),
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
