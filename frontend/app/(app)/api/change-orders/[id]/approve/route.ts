import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, clientIpFrom, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const formData = await request.formData();
  try {
    const response = await fetch(`${BACKEND_API_URL}/change-orders/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      // X-Forwarded-For: the backend records the signer's IP as ESIGN
      // evidence — without forwarding it, every signature would record the
      // frontend container's address (see RequestOptions.clientIp).
      headers: {
        Authorization: `Bearer ${token}`,
        ...(clientIpFrom(request) ? { "X-Forwarded-For": clientIpFrom(request)! } : {}),
      },
      body: formData,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to approve change order" }, { status: 502 });
  }
}
