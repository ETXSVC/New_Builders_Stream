import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/projects/{project_id}/expenses", "get", {
      accessToken: token,
      params: { project_id: id },
      query: {
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load expenses");
  }
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/projects/{project_id}/expenses", "post", {
      accessToken: token,
      params: { project_id: id },
      body,
    });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create expense");
  }
}
