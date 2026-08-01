import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

// A STATIC segment beside `/api/companies/[id]`-shaped routes, and the
// backend registers its router before `companies.router` for the same
// reason — `/companies/{company_id}` would otherwise swallow the literal
// and 422 on a path that is not a UUID.
export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/companies/email-settings", "get", { accessToken: token });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load the mail server settings");
  }
}

export async function PUT(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/companies/email-settings", "put", {
      accessToken: token,
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    // 422 carries the SSRF guard's own words ("resolves to an address on a
    // private or reserved network"), which is the one thing an admin
    // needs to see verbatim.
    return errorResponse(err, "Failed to save the mail server settings");
  }
}

export async function DELETE(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    await apiFetch("/companies/email-settings", "delete", { accessToken: token });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to remove the mail server settings");
  }
}
