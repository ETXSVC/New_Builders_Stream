import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/estimates/{estimate_id}/send-for-signature", "post", {
      accessToken: token,
      params: { estimate_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to send estimate for signature");
  }
}
