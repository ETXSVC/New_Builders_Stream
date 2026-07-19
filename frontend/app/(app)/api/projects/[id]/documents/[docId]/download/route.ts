import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; docId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id, docId } = await params;
  try {
    const upstream = await fetch(
      `${BACKEND_API_URL}/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}/download`,
      { headers: { Authorization: `Bearer ${token}` } }
    );
    if (!upstream.ok) {
      let detail = "Download failed";
      try {
        detail = (await upstream.json()).detail ?? detail;
      } catch {}
      return NextResponse.json({ detail }, { status: upstream.status });
    }
    // Stream the body through, preserving the filename the backend chose.
    return new NextResponse(upstream.body, {
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/octet-stream",
        "Content-Disposition": upstream.headers.get("Content-Disposition") ?? "attachment",
      },
    });
  } catch {
    return NextResponse.json({ detail: "Download failed" }, { status: 502 });
  }
}
