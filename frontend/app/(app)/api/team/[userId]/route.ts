import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { userId } = await params;
  try {
    const data = await apiFetch("/team/{user_id}", "get", {
      accessToken: token,
      params: { user_id: userId },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load this team member");
  }
}

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { userId } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/team/{user_id}", "patch", {
      accessToken: token,
      params: { user_id: userId },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to save this team member");
  }
}
