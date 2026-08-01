import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";

// Pre-auth, and deliberately returns no session: a completed reset sends
// the user to the login form rather than signing them in. The reset also
// revokes every refresh token the account holds, so issuing one here would
// be handing back a session the backend has just decided to distrust.
export async function POST(request: NextRequest) {
  const body = await request.json();
  try {
    await apiFetch("/auth/password-reset/confirm", "post", { body });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    // Passed through so the page can tell "this link is spent or expired"
    // (400) from "your authenticator code is wrong" (401) — two different
    // things for the user to do next.
    return errorResponse(err, "Could not reset the password");
  }
}
