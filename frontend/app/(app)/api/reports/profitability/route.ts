import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();

  // Both are required by the backend, which 422s if start_date is after
  // end_date. Forwarded rather than defaulted here: the page owns the range
  // it is showing, and a BFF quietly substituting a different one would make
  // the figures disagree with the controls that produced them.
  const startDate = request.nextUrl.searchParams.get("start_date") ?? undefined;
  const endDate = request.nextUrl.searchParams.get("end_date") ?? undefined;

  try {
    const data = await apiFetch("/reports/profitability", "get", {
      accessToken: token,
      query: { start_date: startDate, end_date: endDate },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load the profitability report");
  }
}
