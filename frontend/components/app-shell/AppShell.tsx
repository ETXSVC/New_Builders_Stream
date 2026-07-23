"use client";

import * as React from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Nav } from "@/components/app-shell/Nav";

// Pre-auth screens that live inside the (app) route group (they need its
// Tailwind globals and AuthProvider) but must not show the app chrome.
const PRE_AUTH_PATHS = ["/login", "/register"];

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
  const { accessToken, isHydrating } = useAuth();
  const isPreAuthPath = PRE_AUTH_PATHS.includes(pathname);

  React.useEffect(() => {
    if (isPreAuthPath || isHydrating || accessToken !== null) return;
    router.replace("/login");
  }, [isPreAuthPath, isHydrating, accessToken, router]);

  if (isPreAuthPath) return <>{children}</>;

  // Session confirmed gone (or the redirect above hasn't committed yet):
  // render nothing rather than the app chrome + a page that would just
  // silently no-op every fetch against a token that no longer exists.
  if (!isHydrating && accessToken === null) return null;

  return (
    <>
      <Nav companyId={decodeCompanyId(accessToken)} />
      {children}
    </>
  );
}

function decodeCompanyId(accessToken: string | null): string {
  if (!accessToken) return "";
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return payload.default_company_id ?? "";
  } catch {
    return "";
  }
}
