import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ professionId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { professionId } = await params;
  try {
    await apiFetch("/team/professions/{profession_id}", "delete", {
      accessToken: token,
      params: { profession_id: professionId },
    });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to remove the profession");
  }
}
