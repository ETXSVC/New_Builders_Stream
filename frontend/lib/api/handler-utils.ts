import "server-only";

import { NextRequest, NextResponse } from "next/server";
import { ApiError } from "./client";

// The raw "Bearer <token>" header value, passed through to apiFetch's
// accessToken option after stripping the scheme. null → caller should 401.
export function bearerToken(request: NextRequest): string | null {
  const header = request.headers.get("Authorization");
  if (!header?.startsWith("Bearer ")) return null;
  return header.slice(7);
}

export function missingTokenResponse(): NextResponse {
  return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
}

export function errorResponse(err: unknown, fallback: string): NextResponse {
  if (err instanceof ApiError) {
    return NextResponse.json({ detail: err.detail }, { status: err.status });
  }
  return NextResponse.json({ detail: fallback }, { status: 502 });
}
