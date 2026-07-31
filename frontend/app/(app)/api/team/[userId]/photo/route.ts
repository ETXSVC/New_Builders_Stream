/**
 * A team member's photo: multipart in, image bytes out.
 *
 * Both directions bypass `apiFetch`, which is JSON-only — the same reason
 * the branding-logo upload and the estimate-PDF download do. The GET streams
 * `upstream.body` straight through rather than buffering it, and passes the
 * upstream Content-Type along, because the backend picks png or jpeg from
 * what was uploaded.
 */
import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

function photoUrl(userId: string): string {
  return `${BACKEND_API_URL}/team/${encodeURIComponent(userId)}/photo`;
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { userId } = await params;
  try {
    const upstream = await fetch(photoUrl(userId), {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!upstream.ok) {
      let detail = "No photo for this member";
      try {
        detail = (await upstream.json()).detail ?? detail;
      } catch {}
      return NextResponse.json({ detail }, { status: upstream.status });
    }
    return new NextResponse(upstream.body, {
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/octet-stream",
        // Not cacheable by anything shared: this is one tenant's staff
        // photo behind a role check, and the URL is the same for every
        // caller. `private` keeps it out of a proxy without forbidding the
        // browser's own memory cache.
        "Cache-Control": "private, no-store",
      },
    });
  } catch {
    return NextResponse.json({ detail: "No photo for this member" }, { status: 502 });
  }
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ userId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { userId } = await params;
  const formData = await request.formData();
  try {
    const upstream = await fetch(photoUrl(userId), {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    const data = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch {
    return NextResponse.json({ detail: "Failed to upload the photo" }, { status: 502 });
  }
}
