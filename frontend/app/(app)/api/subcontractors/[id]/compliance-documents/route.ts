import { NextRequest, NextResponse } from "next/server";
import { apiFetch, BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/subcontractors/{subcontractor_id}/compliance-documents", "get", {
      accessToken: token,
      params: { subcontractor_id: id },
      query: { cursor: request.nextUrl.searchParams.get("cursor") ?? undefined },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load compliance documents");
  }
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  // Multipart pass-through: apiFetch is JSON-only, so this handler talks to
  // the backend directly — same pattern as the project-documents upload
  // route (still server-side; the BFF boundary holds).
  const formData = await request.formData();
  try {
    const response = await fetch(
      `${BACKEND_API_URL}/subcontractors/${encodeURIComponent(id)}/compliance-documents`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      }
    );
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to upload compliance document" }, { status: 502 });
  }
}
