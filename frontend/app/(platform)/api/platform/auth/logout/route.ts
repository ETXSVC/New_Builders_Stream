import { NextResponse } from "next/server";
import { clearPlatformCookie } from "@/lib/platform/session";

/**
 * Drops the cookie. There is no backend call to make: platform tokens are
 * stateless JWTs with no refresh family to revoke (unlike the product's
 * /auth/logout, which must invalidate a stored refresh token). Privilege is
 * re-checked from `platform_admins` on every request anyway, so a revoked
 * admin loses access mid-session whether or not they sign out.
 */
export async function POST() {
  const response = NextResponse.json({ ok: true });
  clearPlatformCookie(response);
  response.headers.set("Cache-Control", "no-store");
  return response;
}
