import { NextRequest, NextResponse } from "next/server";

/**
 * The platform console's session, which is deliberately NOT the product's.
 *
 * The product keeps its access token in React memory (`contexts/AuthContext`)
 * and re-derives it on every cold load from an httpOnly `refresh_token`
 * cookie. The console cannot do that: `POST /platform/auth/login` returns an
 * access token and **no refresh token**, by design — a credential that
 * reaches every tenant's subscription state is not silently renewable, and
 * re-authenticating means re-entering a TOTP code.
 *
 * So the token itself goes in the cookie. Three things follow, all of them
 * wanted:
 *
 * - **It never touches JavaScript.** `httpOnly` means no XSS on any page of
 *   this origin can read a cross-tenant credential. That is strictly better
 *   than the product path, and appropriate for what this token can do.
 * - **`middleware.ts` can gate `/platform/*` on cookie presence**, exactly
 *   as it already gates the product routes — one mechanism, not two.
 * - **A hard refresh survives; expiry logs you out.** The cookie's lifetime
 *   is the token's own, reported by the backend as `expires_in_minutes`
 *   rather than hardcoded here, so changing
 *   `create_platform_token`'s lifetime does not silently leave the browser
 *   holding a cookie outliving the token inside it.
 *
 * Not `server-only`: `middleware.ts` imports the cookie name from here.
 * Duplicating the string there instead — the existing `REFRESH_COOKIE`
 * pattern — is two places to edit and one to forget.
 */
export const PLATFORM_COOKIE = "platform_token";

export function platformToken(request: NextRequest): string | null {
  return request.cookies.get(PLATFORM_COOKIE)?.value ?? null;
}

export function missingPlatformTokenResponse(): NextResponse {
  return NextResponse.json({ detail: "Not signed in to the platform console" }, { status: 401 });
}

export function setPlatformCookie(
  response: NextResponse,
  token: string,
  expiresInMinutes: number
): void {
  response.cookies.set(PLATFORM_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    // "strict" rather than the product's "lax": nothing should ever
    // navigate into the console from another site, and an administrative
    // surface that changes every tenant's entitlements is the last place to
    // accept a cross-site request.
    sameSite: "strict",
    path: "/",
    maxAge: expiresInMinutes * 60,
  });
}

export function clearPlatformCookie(response: NextResponse): void {
  response.cookies.set(PLATFORM_COOKIE, "", {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: 0,
  });
}
