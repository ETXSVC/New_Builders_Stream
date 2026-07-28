import { test, expect } from "@playwright/test";

/**
 * The security headers in next.config.ts, asserted rather than assumed.
 *
 * These were added during the production-hardening work and nothing has
 * ever checked them. That mattered the moment one of them needed to
 * differ by environment: `next dev` requires `'unsafe-eval'` (React's
 * development build calls eval() for debugging machinery, and Turbopack's
 * HMR runtime does too), and a production build must never have it.
 *
 * A dev-only relaxation that silently reaches production is worse than
 * never having relaxed it — the CSP would still be *present*, still look
 * right in review, and no longer stop the thing it exists to stop. So the
 * absence of `'unsafe-eval'` is the assertion this file exists for.
 */

const EXPECTED = {
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "strict-origin-when-cross-origin",
} as const;

async function headersFor(request: import("@playwright/test").APIRequestContext, path = "/login") {
  const response = await request.get(path, { maxRedirects: 0 });
  return response.headers();
}

test.describe("security headers", () => {
  test("every header next.config.ts declares is actually served", async ({ request }) => {
    const headers = await headersFor(request);

    for (const [name, value] of Object.entries(EXPECTED)) {
      expect(headers[name], `${name} header`).toBe(value);
    }
    expect(headers["permissions-policy"]).toContain("camera=()");
    expect(headers["permissions-policy"]).toContain("microphone=()");
    expect(headers["permissions-policy"]).toContain("geolocation=()");
    // poweredByHeader: false
    expect(headers["x-powered-by"]).toBeUndefined();
  });

  test("the CSP keeps the directives the rest of the design depends on", async ({ request }) => {
    const csp = (await headersFor(request))["content-security-policy"];
    expect(csp, "no CSP served at all").toBeTruthy();

    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    // The estimate PDF preview is an <iframe> pointing at a blob: URL
    // (PdfPanel.tsx). `frame-src` falls back through `child-src` to
    // `default-src 'self'`, which does not cover blob: — omitting this
    // directive replaced the preview with the browser's "This content is
    // blocked" placeholder, and the app reported nothing because nothing
    // in the app had failed.
    expect(csp, "frame-src must allow blob: or the PDF preview is blocked").toContain(
      "frame-src 'self' blob:"
    );
    expect(csp).toContain("object-src 'none'");
    // connect-src 'self' is load-bearing beyond itself: it is the reason
    // Sentry is tunnelled through /monitoring rather than posting to
    // ingest.sentry.io (see next.config.ts). Widening it would quietly
    // remove the justification for that whole arrangement.
    expect(csp).toContain("connect-src 'self'");
  });

  test("a production build never allows 'unsafe-eval'", async ({ request }) => {
    // Gated on CI for the same reason `failOnFlakyTests` is in
    // playwright.config.ts: the merge gate builds and serves a PRODUCTION
    // frontend (`npm run build && npm run start` in e2e-ci.yml), which is
    // the only place this assertion is meaningful. Locally the suite
    // usually runs against `next dev`, where 'unsafe-eval' is present on
    // purpose and failing here would just train people to ignore it.
    //
    // Set CI=1 locally against a production build to reproduce the gate.
    test.skip(
      !process.env.CI,
      "runs against the production build in CI; `next dev` legitimately allows eval"
    );

    const csp = (await headersFor(request))["content-security-policy"];
    expect(
      csp,
      "'unsafe-eval' reached the production CSP — it is a development-only " +
        "relaxation for React's debug build and Turbopack HMR (next.config.ts)"
    ).not.toContain("unsafe-eval");
  });
});
