import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ provider: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { provider } = await params;
  try {
    const data = await apiFetch("/integrations/{provider}/sync-status", "get", {
      accessToken: token,
      params: { provider },
      query: {
        status: request.nextUrl.searchParams.get("status") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load sync status");
  }
}
