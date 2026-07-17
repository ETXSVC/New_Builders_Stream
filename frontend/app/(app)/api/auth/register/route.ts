import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function POST(request: NextRequest) {
  const body = await request.json();
  try {
    const data = await apiFetch("/auth/register", "post", { body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Registration failed" }, { status: 502 });
  }
}
