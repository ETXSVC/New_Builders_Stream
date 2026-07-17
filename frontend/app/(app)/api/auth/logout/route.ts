import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";

const REFRESH_COOKIE = "refresh_token";

export async function POST(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (refreshToken) {
    // Backend logout is deliberately idempotent/always-204 (spec Decision
    // 5 of the token-lifecycle design) — no need to inspect the result.
    await apiFetch("/auth/logout", "post", { body: { refresh_token: refreshToken } }).catch(() => {});
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.delete(REFRESH_COOKIE);
  return response;
}
