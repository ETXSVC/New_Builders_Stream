import type { NextConfig } from "next";

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

export default nextConfig;
