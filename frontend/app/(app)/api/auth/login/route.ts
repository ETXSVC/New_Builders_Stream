import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";
const REFRESH_COOKIE_MAX_AGE_SECONDS = 14 * 24 * 60 * 60; // 14 days, matches the backend's refresh lifetime

export async function POST(request: NextRequest) {
  const body = await request.json();

  try {
    const data = (await apiFetch("/auth/login", "post", { body })) as {
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
    // RFC 6749 §5.1: responses carrying tokens must not be cached — the
    // backend sets this on its own /auth/login response, but apiFetch
    // only ever returns the parsed body, discarding the backend
    // Response's headers, so it doesn't propagate automatically and must
    // be re-set here (same pattern Task 12's mfa/enroll route uses).
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Login failed" }, { status: 502 });
  }
}
