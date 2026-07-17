import { NextRequest, NextResponse } from "next/server";

const REFRESH_COOKIE = "refresh_token";

// The `matcher` config below is the ONLY scope boundary — it's Next.js's
// own path-to-regexp match, segment-safe by construction (":path*"
// requires an exact match or a "/" before further segments, so a future
// route like "/accounting" or "/dashboards" is never matched and this
// function never runs for it). Keep it that way: an in-function prefix
// re-check here would just be a second, WEAKER copy of the same rule
// (a raw startsWith is not segment-safe) that could silently drift out
// of sync with `matcher` if either is ever edited without the other.
export function middleware(request: NextRequest) {
  const hasSession = request.cookies.has(REFRESH_COOKIE);
  if (!hasSession) {
    const loginUrl = new URL("/login", request.url);
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/account/:path*"],
};
