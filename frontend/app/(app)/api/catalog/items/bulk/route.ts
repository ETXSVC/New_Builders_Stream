import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items/bulk", "post", { accessToken: token, body });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to import catalog items");
  }
}
