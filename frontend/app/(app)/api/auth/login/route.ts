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
    };

    const response = NextResponse.json({
      access_token: data.access_token,
      default_company_id: data.default_company_id,
      mfa_enrollment_required: data.mfa_enrollment_required,
    });
    response.cookies.set(REFRESH_COOKIE, data.refresh_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: REFRESH_COOKIE_MAX_AGE_SECONDS,
    });
    return response;
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Login failed" }, { status: 502 });
  }
}
