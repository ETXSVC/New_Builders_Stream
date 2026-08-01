import { NextRequest, NextResponse } from "next/server";
import { PLATFORM_COOKIE } from "@/lib/platform/session";

const REFRESH_COOKIE = "refresh_token";
const PLATFORM_LOGIN_PATH = "/platform/login";

/**
 * TWO JOBS, TWO SCOPES, AND WHY THAT IS NOT THE MISTAKE IT LOOKS LIKE.
 *
 * This file used to do one thing — redirect a signed-out visitor — and its
 * `matcher` was the ONLY expression of which paths that applied to. That
 * was deliberate: `matcher` is Next's own path-to-regexp match, segment
 * safe by construction (":path*" needs an exact match or a "/" before
 * further segments, so "/accounting" never matches "/account"), and an
 * in-function `startsWith` re-check would have been a second, WEAKER copy
 * of the same rule, free to drift.
 *
 * The nonce-based CSP forced the issue. A nonce must be minted per request
 * and reach the HTML of EVERY page, so the matcher below now covers
 * everything that is not a static asset — which means it can no longer
 * double as the auth scope.
 *
 * So the auth scope moved into `PROTECTED_TREES` and is matched with the
 * same segment-safe rule `matcher` used, written once in `isUnder()`:
 * exact match, or a "/" immediately after the prefix. `/accounting` still
 * does not match `/account`, and there is exactly one implementation of
 * that test rather than two dialects of it. The list is the boundary now;
 * keep it in sync with the routes that exist, and note that a route NOT in
 * it is public.
 */
const PROTECTED_TREES = [
  "/dashboard",
  "/account",
  "/leads",
  "/projects",
  "/my-tasks",
  "/estimates",
  "/catalog",
  "/materials",
  "/billing",
  "/compliance",
  "/subcontractors",
  "/team",
  "/integrations",
  "/platform",
];

/** Exact match, or the prefix followed by "/" — never a bare startsWith. */
function isUnder(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(prefix + "/");
}

/**
 * The CSP, built per request around a fresh nonce.
 *
 * `'strict-dynamic'` is what makes the nonce worth having: scripts loaded
 * BY a nonced script inherit its trust, which is how Next's chunk loader
 * keeps working without listing every chunk. Under CSP3 it also makes
 * `'self'` and any host allowlist ignored for scripts, so an injected
 * `<script src>` is refused however it got onto the page.
 *
 * `'unsafe-inline'` stays in the script-src list on purpose: CSP3 browsers
 * ignore it once a nonce or `'strict-dynamic'` is present, and CSP2-only
 * browsers ignore the nonce and fall back to it rather than breaking the
 * app entirely. It is a compatibility floor, not a hole in the policy for
 * anything modern.
 *
 * `style-src` keeps `'unsafe-inline'` for real: Next and Tailwind both
 * inject style tags, and there is no style nonce pipeline here. Styles are
 * a far narrower vector than scripts, and pretending otherwise by removing
 * it would just break the app.
 *
 * `'unsafe-eval'` remains development-only — React's dev build and
 * Turbopack's HMR runtime call eval(), production never does, and
 * `e2e/security-headers.spec.ts` asserts its absence from the real
 * production artifact.
 */
function contentSecurityPolicy(nonce: string, isDevelopment: boolean): string {
  const scriptSrc = [
    "script-src 'self'",
    `'nonce-${nonce}'`,
    "'strict-dynamic'",
    "'unsafe-inline'",
    isDevelopment ? "'unsafe-eval'" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return [
    "default-src 'self'",
    scriptSrc,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    // The estimate PDF preview is `<iframe src={URL.createObjectURL(...)}>`
    // (components/estimates/PdfPanel.tsx): the PDF is fetched with the
    // bearer token and turned into a blob, because an <iframe> cannot carry
    // an Authorization header. `frame-src` falls back through `child-src`
    // to `default-src 'self'`, which does NOT cover `blob:` — omitting this
    // replaced the panel with the browser's "This content is blocked"
    // message, with the app reporting nothing because nothing in it failed.
    "frame-src 'self' blob:",
    // Nothing here uses <object>/<embed>; 'none' removes the surface.
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    // Worth having alongside a nonce: without it, an injected <form> can
    // post the page's fields anywhere.
    "form-action 'self'",
  ].join("; ");
}

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // 16 bytes of CSPRNG, base64. `crypto` is the Web Crypto global in the
  // Edge runtime — `node:crypto` is not available here.
  const nonce = Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString("base64");
  const csp = contentSecurityPolicy(nonce, process.env.NODE_ENV === "development");

  // Set on the REQUEST as well as the response, and this is the part that
  // does the work: Next reads the nonce out of the request's own CSP header
  // and stamps it onto the inline bootstrap scripts it emits. Without it
  // the header would be a nonce nothing carries, and every page would fail
  // to hydrate under its own policy.
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const withCsp = (response: NextResponse) => {
    response.headers.set("Content-Security-Policy", csp);
    return response;
  };

  const isProtected = PROTECTED_TREES.some((prefix) => isUnder(pathname, prefix));

  // The console's login page must stay reachable while signed out, or the
  // console is unenterable.
  if (isProtected && pathname !== PLATFORM_LOGIN_PATH) {
    // Two protected trees holding two different credentials, so this does
    // have to know which tree it is in. Getting it wrong sends a logged-out
    // operator to the tenant login screen rather than granting anything.
    const isPlatform = isUnder(pathname, "/platform");
    const cookie = isPlatform ? PLATFORM_COOKIE : REFRESH_COOKIE;

    if (!request.cookies.has(cookie)) {
      // Cookie PRESENCE only, same as it has always been here — validity is
      // the backend's business, and for the console that means an expired
      // token 401s at the BFF and the page sends the operator back here.
      return withCsp(
        NextResponse.redirect(new URL(isPlatform ? PLATFORM_LOGIN_PATH : "/login", request.url))
      );
    }
  }

  return withCsp(NextResponse.next({ request: { headers: requestHeaders } }));
}

export const config = {
  matcher: [
    /*
     * Everything except static assets and the BFF routes, because the CSP
     * has to reach every DOCUMENT. Excluded here:
     *   _next/static, _next/image — build output, served with their own
     *     headers and no inline script of ours;
     *   api — Route Handlers return JSON, not HTML; a CSP on them protects
     *     nothing and the auth checks there are the backend's;
     *   favicon.ico, images — static files.
     *
     * `missing` skips prefetch requests, whose responses are never
     * documents and would otherwise mint a nonce nothing uses.
     */
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico|images).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
