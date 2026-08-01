import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

// Sends one real message through the tenant's own server, to the signed-in
// admin. A failure to connect or authenticate comes back as 200 with
// `ok: false` and the relay's own words — that is a normal outcome of
// testing a configuration, not an API error.
export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/companies/email-settings/test", "post", {
      accessToken: token,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Could not send the test message");
  }
}
