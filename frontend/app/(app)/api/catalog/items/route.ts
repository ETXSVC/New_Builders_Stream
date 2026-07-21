import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/catalogs/items", "get", {
      accessToken: token,
      query: {
        category: request.nextUrl.searchParams.get("category") ?? undefined,
        search: request.nextUrl.searchParams.get("search") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load catalog items");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create catalog item");
  }
}
