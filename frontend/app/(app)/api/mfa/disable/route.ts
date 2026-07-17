import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";

export async function POST(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
  const body = await request.json();
  try {
    await apiFetch("/auth/mfa/disable", "post", { accessToken: authHeader.replace("Bearer ", ""), body });
    // Disabling MFA revokes every refresh-token session the user holds
    // (backend/app/routers/auth.py's mfa_disable, confirmed against
    // docs/superpowers/specs/2026-07-16-mfa-totp-design.md's Decision 6)
    // — including this browser's own cookie. The client must treat this
    // as a forced logout, not just an in-place state update.
    const response = new NextResponse(null, { status: 204 });
    response.cookies.delete(REFRESH_COOKIE);
    return response;
  } catch (err) {
    if (err instanceof ApiError) return NextResponse.json({ detail: err.detail }, { status: err.status });
    return NextResponse.json({ detail: "Disable failed" }, { status: 502 });
  }
}
