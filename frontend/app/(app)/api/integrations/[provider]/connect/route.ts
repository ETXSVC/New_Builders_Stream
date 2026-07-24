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
    const data = await apiFetch("/integrations/{provider}/connect", "get", {
      accessToken: token,
      params: { provider },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to start the connection");
  }
}
