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

// First-hop client IP from the reverse proxy's X-Forwarded-For. Caddy (the
// production proxy) discards any client-supplied XFF and sets its own, so
// the first entry is the true client address and spoof-safe. Returns
// undefined when no proxy is in front (bare `npm run dev`), in which case
// callers omit the forwarded header entirely.
export function clientIpFrom(request: NextRequest): string | undefined {
  return request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || undefined;
}
