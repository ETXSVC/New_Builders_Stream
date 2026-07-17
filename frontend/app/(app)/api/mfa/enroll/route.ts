import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
  try {
    const data = await apiFetch("/auth/mfa/enroll", "post", {
      accessToken: authHeader.replace("Bearer ", ""),
    });
    const response = NextResponse.json(data);
    // RFC 6749 §5.1: this response carries the TOTP shared secret in its body.
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (err) {
    if (err instanceof ApiError) return NextResponse.json({ detail: err.detail }, { status: err.status });
    return NextResponse.json({ detail: "Enrollment failed" }, { status: 502 });
  }
}
