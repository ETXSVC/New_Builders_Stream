import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";
import { clientIpFrom } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest) {
  const body = await request.json();
  try {
    // clientIp: the register rate limiter keys per client IP — see
    // RequestOptions.clientIp in lib/api/client.ts.
    const data = await apiFetch("/auth/register", "post", { body, clientIp: clientIpFrom(request) });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Registration failed" }, { status: 502 });
  }
}
