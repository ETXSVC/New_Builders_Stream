import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/companies/branding", "get", { accessToken: token });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load branding");
  }
}

export async function PUT(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/companies/branding", "put", { accessToken: token, body });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to save branding");
  }
}
