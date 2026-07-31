/**
 * The company's trade list.
 *
 * A STATIC segment beside `[userId]`, which Next resolves first — so
 * `/api/team/professions` reaches this file rather than the member route.
 * The backend needs its professions routes DECLARED before `/team/{user_id}`
 * to get the same outcome, because Starlette matches in registration order;
 * the two ends agree, for different reasons.
 */
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/team/professions", "get", { accessToken: token });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load professions");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/team/professions", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to add the profession");
  }
}
