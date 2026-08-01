import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { clientIpFrom, errorResponse } from "@/lib/api/handler-utils";

// Pre-auth by design, like register and invitation-accept: somebody who has
// forgotten their password has no session to present.
//
// `clientIp` is forwarded because this route is rate limited per source
// address on the backend, and without it every request would arrive from
// the BFF container — collapsing a per-attacker limit into one global
// counter that legitimate users would trip.
export async function POST(request: NextRequest) {
  const body = await request.json();
  try {
    const data = await apiFetch("/auth/password-reset/request", "post", {
      body,
      clientIp: clientIpFrom(request),
    });
    return NextResponse.json(data, { status: 202 });
  } catch (err) {
    // The backend answers 202 whether or not the address is registered, so
    // an error here is a real failure (or a 429) rather than "no such
    // account" — passing it through discloses nothing.
    return errorResponse(err, "Could not send the reset link");
  }
}
