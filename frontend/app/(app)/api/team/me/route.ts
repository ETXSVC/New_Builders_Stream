/**
 * Your own record.
 *
 * A static segment beside `[userId]`, which Next resolves first — and the
 * backend's `/team/me` is declared before its own `/{user_id}` for the same
 * outcome by a different rule (Starlette matches in registration order).
 *
 * No id in the path on purpose: the product session carries a token and a
 * role, never a user id, so the browser has none to send.
 */
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/team/me", "get", { accessToken: token });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load your profile");
  }
}

export async function PATCH(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/team/me", "patch", { accessToken: token, body });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to save your profile");
  }
}
