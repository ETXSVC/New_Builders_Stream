import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/esignatures/{esignature_id}", "get", {
      accessToken: token,
      params: { esignature_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load signature record");
  }
}
