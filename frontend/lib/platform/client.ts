"use client";

/**
 * Browser-side fetch for the console's BFF routes.
 *
 * There is no token to attach: it lives in an httpOnly cookie the browser
 * sends automatically (`lib/platform/session.ts`). What this adds is one
 * consistent reaction to a 401, which for the console means one of two
 * things — the 30-minute token expired, or the operator's privilege was
 * revoked mid-session (`platform_admins` is re-read on every request, so
 * revoking bites within one request rather than one token lifetime).
 *
 * Both mean "sign in again", so both get a full navigation rather than a
 * client-side push: it discards whatever tenant state the page was holding,
 * which a soft navigation would leave sitting on screen next to a login
 * form.
 *
 * The common expiry case never reaches here — the cookie's lifetime is the
 * token's, so an operator returning to a stale tab is redirected by
 * `middleware.ts` on their next navigation. This covers the tab left open
 * across the boundary that then tries to act.
 */
/**
 * The one reaction to "this session is over", exported so the surfaces that
 * do NOT go through `platformFetch` react identically.
 *
 * `TenantList` is the case: it loads through `useCursorListCore`, which owns
 * its own fetch, so without this it would render "Failed to load tenants"
 * next to a console the operator is no longer signed in to — on the console's
 * home page, which is the one most likely to be sitting open when a
 * privilege is revoked.
 */
export function endPlatformSession(): void {
  window.location.assign("/platform/login");
}

export async function platformFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);

  if (response.status === 401) {
    endPlatformSession();
    throw new Error("Your console session has ended. Sign in again.");
  }

  const data = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(data?.detail ?? "The request failed.");
  }
  return data as T;
}

export async function platformSignOut(): Promise<void> {
  await fetch("/api/platform/auth/logout", { method: "POST" });
  endPlatformSession();
}
