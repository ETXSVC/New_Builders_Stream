import { NextRequest, NextResponse } from "next/server";
import { apiFetch, BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/projects/{project_id}/documents", "get", {
      accessToken: token,
      params: { project_id: id },
      query: { cursor: request.nextUrl.searchParams.get("cursor") ?? undefined },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load documents");
  }
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  // Multipart pass-through: apiFetch is JSON-only, so this handler talks to
  // the backend directly (still server-side — the BFF boundary holds; the
  // browser never reaches the backend origin).
  const formData = await request.formData();
  try {
    const response = await fetch(`${BACKEND_API_URL}/projects/${encodeURIComponent(id)}/documents`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to upload document" }, { status: 502 });
  }
}
