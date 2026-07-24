import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; phaseId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id, phaseId } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/projects/{project_id}/phases/{phase_id}", "patch", {
      accessToken: token,
      params: { project_id: id, phase_id: phaseId },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update phase");
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; phaseId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id, phaseId } = await params;
  try {
    await apiFetch("/projects/{project_id}/phases/{phase_id}", "delete", {
      accessToken: token,
      params: { project_id: id, phase_id: phaseId },
    });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to delete phase");
  }
}
