import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";

// Deliberately no bearerToken/missingTokenResponse: the backend's
// invitation-accept endpoint is pre-auth by design (the invitee has no
// account yet — accepting is what creates it), same category as
// /api/auth/register above. Backend errors pass through so the page can
// map them: 404 invalid, 409 already accepted, 410 expired.
export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/invitations/{invitation_id}/accept", "post", {
      params: { invitation_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to accept invitation");
  }
}
