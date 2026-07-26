import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/vendors", "get", {
      accessToken: token,
      query: {
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load vendors");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/vendors", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create vendor");
  }
}
