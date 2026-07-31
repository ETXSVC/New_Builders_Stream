import { NextRequest, NextResponse } from "next/server";
import { PLATFORM_COOKIE } from "@/lib/platform/session";

const REFRESH_COOKIE = "refresh_token";
const PLATFORM_LOGIN_PATH = "/platform/login";

// The `matcher` config below is the ONLY scope boundary — it's Next.js's
// own path-to-regexp match, segment-safe by construction (":path*"
// requires an exact match or a "/" before further segments, so a future
// route like "/accounting" or "/dashboards" is never matched and this
// function never runs for it). Keep it that way: an in-function prefix
// re-check here would just be a second, WEAKER copy of the same rule
// (a raw startsWith is not segment-safe) that could silently drift out
// of sync with `matcher` if either is ever edited without the other.
export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // The console's login page must stay reachable while signed out, or the
  // console is unenterable. Checked before the branch below because it is
  // the one matched path that is deliberately public.
  if (pathname === PLATFORM_LOGIN_PATH) return NextResponse.next();

  // Two protected trees holding two different credentials, so this function
  // does have to know which tree it is in. That is NOT the weaker second
  // copy of `matcher` warned about above: it selects between two rules
  // rather than re-deriving the scope of one, and getting it wrong sends a
  // logged-out operator to the tenant login screen rather than granting
  // anything.
  const isPlatform = pathname === "/platform" || pathname.startsWith("/platform/");
  const cookie = isPlatform ? PLATFORM_COOKIE : REFRESH_COOKIE;

  if (!request.cookies.has(cookie)) {
    // Cookie PRESENCE only, same as it has always been here — validity is
    // the backend's business, and for the console that means an expired
    // token 401s at the BFF and the page sends the operator back here.
    return NextResponse.redirect(new URL(isPlatform ? PLATFORM_LOGIN_PATH : "/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    "/dashboard/:path*",
    "/account/:path*",
    "/leads/:path*",
    "/projects/:path*",
    "/my-tasks/:path*",
    "/estimates/:path*",
    "/catalog/:path*",
    "/materials/:path*",
    "/billing/:path*",
    "/compliance/:path*",
    "/subcontractors/:path*",
    "/team/:path*",
    "/integrations/:path*",
    // The platform console. ":path*" matches zero or more segments, so this
    // one entry covers "/platform" itself as well as everything under it —
    // including "/platform/login", which the function above lets through.
    "/platform/:path*",
  ],
};
