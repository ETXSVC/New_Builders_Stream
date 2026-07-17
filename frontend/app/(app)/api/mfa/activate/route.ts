import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  if (!authHeader) return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
  const body = await request.json();
  try {
    await apiFetch("/auth/mfa/activate", "post", { accessToken: authHeader.replace("Bearer ", ""), body });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    if (err instanceof ApiError) return NextResponse.json({ detail: err.detail }, { status: err.status });
    return NextResponse.json({ detail: "Activation failed" }, { status: 502 });
  }
}
