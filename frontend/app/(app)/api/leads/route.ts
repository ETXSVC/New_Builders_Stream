import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/leads", "get", {
      accessToken: token,
      query: {
        status: request.nextUrl.searchParams.get("status") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
        limit: request.nextUrl.searchParams.get("limit") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load leads");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/leads", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create lead");
  }
}
