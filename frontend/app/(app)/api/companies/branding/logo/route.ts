import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const formData = await request.formData();
  try {
    const response = await fetch(`${BACKEND_API_URL}/companies/branding/logo`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to upload logo" }, { status: 502 });
  }
}
