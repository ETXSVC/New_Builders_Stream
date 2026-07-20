import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/tasks", "get", {
      accessToken: token,
      query: { assignee: "me" },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load tasks");
  }
}
