"use client";

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Nav } from "@/components/app-shell/Nav";
import { readCompanyId } from "@/lib/offline/identity";

// Pre-auth screens that live inside the (app) route group (they need its
// Tailwind globals and AuthProvider) but must not show the app chrome.
// `/forgot-password` and `/reset-password` are here for the reason the
// whole list exists, and they are the two that prove it matters: a person
// following a reset link from their inbox has, by definition, no session —
// so without this entry the shell would replace the page with /login the
// moment hydration confirmed that, and the feature would be unreachable
// for exactly the people it is for. Caught by e2e/foundation.spec.ts, not
// by review.
const PRE_AUTH_PATHS = [
  "/login",
  "/register",
  "/accept-invitation",
  "/forgot-password",
  "/reset-password",
];

// Mounts the shared Nav above every authenticated app screen. Rendered by
// the (app) layout so role-landing pages (field_crew → /my-tasks, client →
// /projects) get navigation and logout without each page wiring Nav itself.
//
// Also owns redirecting to /login the moment the session is confirmed gone
// — not just absent at page-load. `middleware.ts` only checks the refresh
// cookie's presence, and only at NAVIGATION time; it never re-runs for an
// already-loaded page whose session dies later (a scheduled token refresh
// failing — expired/revoked refresh cookie, network blip — calls
// AuthContext's `clearSession()`, per that file's own `scheduleRefresh`).
// Before this fix, `clearSession()` only cleared in-memory state: nothing
// navigated anywhere, and nearly every data-fetching/mutating call in the
// app already silently no-ops on a null `accessToken` (the established
// `if (!accessToken) return;` guard used throughout), so the user was left
// on the same page with stale data and every button doing nothing —
// no error, no prompt to log back in, until they happened to reload or
// navigate by hand. Gated on `!isHydrating`: during the brief cold-load
// window (bookmark, hard refresh, new tab) `accessToken` starts null even
// when a valid refresh cookie exists (AuthContext's own hydration
// comment) — redirecting during that window would bounce a genuinely
// logged-in user to /login for no reason.
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { accessToken, isHydrating, sessionUnreachable } = useAuth();
  const isPreAuthPath = PRE_AUTH_PATHS.includes(pathname);
  // No token, because the server could not be REACHED — not because it said
  // no. On a path the service worker can serve from cache, that is an
  // estimator with no signal, and sending them to /login would replace the
  // one screen that works offline with the one that cannot.
  const strandedOffline =
    sessionUnreachable && accessToken === null && OFFLINE_CAPABLE_PATHS.includes(pathname);

  React.useEffect(() => {
    if (isPreAuthPath || isHydrating || accessToken !== null || strandedOffline) return;
    router.replace("/login");
  }, [isPreAuthPath, isHydrating, accessToken, router, strandedOffline]);

  if (isPreAuthPath) return <>{children}</>;

  // Session confirmed gone (or the redirect above hasn't committed yet):
  // render nothing rather than the app chrome + a page that would just
  // silently no-op every fetch against a token that no longer exists.
  if (!isHydrating && accessToken === null && !strandedOffline) return null;

  return (
    <>
      <Nav companyId={readCompanyId(accessToken)} />
      {children}
    </>
  );
}

// Screens a service worker can serve from cache, which are the only ones
// that can be open with no network at all. Keep in step with `sw.js`'s own
// OFFLINE_PATHS — a path cached there but missing here cold-starts into a
// redirect to /login, which is the feature not working.
const OFFLINE_CAPABLE_PATHS = ["/estimates/capture"];

/*
 * Why `sessionUnreachable` and not `navigator.onLine`:
 *
 * An offline cold start cannot produce an access token — it lives in memory
 * only, and re-deriving it means exchanging the refresh cookie over the
 * network. So `accessToken === null` after hydration, which is exactly the
 * condition the redirect above treats as a confirmed-dead session, and the
 * cached capture screen would load, hydrate, and immediately replace itself
 * with /login.
 *
 * The first version of this guard asked `navigator.onLine`. That flag reads
 * `true` with no route to anything — behind a captive portal, on a Wi-Fi
 * that is up but dead, and under Playwright's own offline emulation, which
 * is how it was caught here rather than in the field. AuthContext's
 * `sessionUnreachable` is evidence instead of a claim: a request was made
 * and did not arrive.
 *
 * Narrow in both remaining directions: only on a path the worker caches,
 * and only to SUPPRESS a redirect. The moment a refresh gets through and is
 * refused, `sessionUnreachable` goes false and the ordinary rule sends them
 * to /login — over a network they can actually sign in on.
 */
