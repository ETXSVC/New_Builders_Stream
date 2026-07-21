import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const upstream = await fetch(`${BACKEND_API_URL}/estimates/${encodeURIComponent(id)}/pdf`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!upstream.ok) {
      let detail = "PDF not available";
      try {
        detail = (await upstream.json()).detail ?? detail;
      } catch {}
      return NextResponse.json({ detail }, { status: upstream.status });
    }
    return new NextResponse(upstream.body, {
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/pdf",
        "Content-Disposition": upstream.headers.get("Content-Disposition") ?? "attachment",
      },
    });
  } catch {
    return NextResponse.json({ detail: "PDF not available" }, { status: 502 });
  }
}
