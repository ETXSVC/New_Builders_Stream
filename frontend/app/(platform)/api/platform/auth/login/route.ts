import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";
import { setPlatformCookie } from "@/lib/platform/session";

export async function POST(request: NextRequest) {
  const body = await request.json();

  try {
    const data = (await apiFetch("/platform/auth/login", "post", { body })) as {
      access_token: string;
      token_type: string;
      expires_in_minutes: number;
      email: string;
    };

    // The token itself is NOT returned to the browser — it goes straight
    // into the httpOnly cookie and never reaches JavaScript. The client only
    // learns who it signed in as, which is all the header needs.
    const response = NextResponse.json({
      email: data.email,
      expires_in_minutes: data.expires_in_minutes,
    });
    setPlatformCookie(response, data.access_token, data.expires_in_minutes);
    // RFC 6749 §5.1: token responses must not be cached. apiFetch returns
    // only the parsed body and discards the backend Response's headers, so
    // the backend's own no-store does not propagate across the BFF hop —
    // same reason /api/auth/login re-sets it.
    response.headers.set("Cache-Control", "no-store");
    return response;
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Login failed" }, { status: 502 });
  }
}
