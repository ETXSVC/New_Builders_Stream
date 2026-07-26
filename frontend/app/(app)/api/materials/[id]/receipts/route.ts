import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/materials/{bom_line_id}/receipts", "post", {
      accessToken: token,
      params: { bom_line_id: id },
      body,
    });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to record receipt");
  }
}
