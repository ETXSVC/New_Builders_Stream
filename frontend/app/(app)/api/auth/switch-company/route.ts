import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";
const REFRESH_COOKIE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60;

/**
 * Act as a different company the signed-in user belongs to.
 *
 * Shaped exactly like /api/auth/refresh, because it IS a refresh: the
 * backend re-mints the session so the access token itself names the active
 * company (see POST /auth/switch-company for why that beats threading an
 * X-Tenant-ID header through every route handler). So the rotated refresh
 * token has to land back in the httpOnly cookie here, or the next scheduled
 * refresh would present a token that was already spent and log the user out.
 *
 * The cookie is deliberately NOT cleared on failure, unlike refresh's error
 * path: a 403 here means "you are not a member of that company", which says
 * nothing about the session's validity — and the backend does not spend the
 * token on that path, so the cookie's value is still the live one.
 */
export async function POST(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!refreshToken) {
    return NextResponse.json({ detail: "No session" }, { status: 401 });
  }

  let companyId: string | undefined;
  try {
    companyId = (await request.json())?.company_id;
  } catch {
    companyId = undefined;
  }
  if (!companyId) {
    return NextResponse.json({ detail: "company_id is required" }, { status: 400 });
  }

  try {
    const data = (await apiFetch("/auth/switch-company", "post", {
      body: { refresh_token: refreshToken, company_id: companyId },
    })) as {
      access_token: string;
      refresh_token: string;
      default_company_id: string;
      mfa_enrollment_required: boolean;
      role: string;
    };

    const response = NextResponse.json({
      access_token: data.access_token,
      default_company_id: data.default_company_id,
      mfa_enrollment_required: data.mfa_enrollment_required,
      role: data.role,
    });
    response.cookies.set(REFRESH_COOKIE, data.refresh_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: REFRESH_COOKIE_MAX_AGE_SECONDS,
    });
    // RFC 6749 §5.1 — see refresh/route.ts's identical comment.
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (err) {
    return NextResponse.json(
      { detail: err instanceof ApiError ? err.detail : "Could not switch company" },
      { status: err instanceof ApiError ? err.status : 502 }
    );
  }
}
