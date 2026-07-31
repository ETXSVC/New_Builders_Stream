/**
 * Your own photo: multipart in, image bytes out.
 *
 * The same shape as `[userId]/photo`, against the self route — which exists
 * because the directory's read roles exclude field crew, who can still edit
 * their own record.
 */
import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

const PHOTO_URL = `${BACKEND_API_URL}/team/me/photo`;

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const upstream = await fetch(PHOTO_URL, { headers: { Authorization: `Bearer ${token}` } });
    if (!upstream.ok) {
      let detail = "No photo yet";
      try {
        detail = (await upstream.json()).detail ?? detail;
      } catch {}
      return NextResponse.json({ detail }, { status: upstream.status });
    }
    return new NextResponse(upstream.body, {
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/octet-stream",
        // Private and unstored, like the directory's copy: same bytes, same
        // reasons — one tenant's staff photo behind a role check.
        "Cache-Control": "private, no-store",
      },
    });
  } catch {
    return NextResponse.json({ detail: "No photo yet" }, { status: 502 });
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const formData = await request.formData();
  try {
    const upstream = await fetch(PHOTO_URL, {
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
