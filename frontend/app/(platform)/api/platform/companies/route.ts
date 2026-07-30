import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";
import { missingPlatformTokenResponse, platformToken } from "@/lib/platform/session";

export async function GET(request: NextRequest) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  try {
    const data = await apiFetch("/platform/companies", "get", {
      accessToken: token,
      query: {
        search: request.nextUrl.searchParams.get("search") ?? undefined,
        roots_only: request.nextUrl.searchParams.get("roots_only") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
        limit: request.nextUrl.searchParams.get("limit") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load tenants");
  }
}
