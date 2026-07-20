import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";
const REFRESH_COOKIE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60;

export async function POST(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!refreshToken) {
    return NextResponse.json({ detail: "No session" }, { status: 401 });
  }

  try {
    const data = (await apiFetch("/auth/refresh", "post", {
      body: { refresh_token: refreshToken },
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
    // Rotation (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md
    // Decision 4): every refresh replaces the cookie with the new token.
    response.cookies.set(REFRESH_COOKIE, data.refresh_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: REFRESH_COOKIE_MAX_AGE_SECONDS,
    });
    // RFC 6749 §5.1 — see login/route.ts's identical comment.
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (err) {
    const response = NextResponse.json(
      { detail: err instanceof ApiError ? err.detail : "Refresh failed" },
      { status: err instanceof ApiError ? err.status : 502 }
    );
    // A dead refresh token (expired/revoked/reused) can never succeed
    // again — clear the cookie so the client doesn't keep retrying it.
    response.cookies.delete(REFRESH_COOKIE);
    return response;
  }
}
