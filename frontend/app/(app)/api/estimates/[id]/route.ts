import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/estimates/{estimate_id}", "get", {
      accessToken: token,
      params: { estimate_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load estimate");
  }
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/estimates/{estimate_id}", "patch", {
      accessToken: token,
      params: { estimate_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update estimate");
  }
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    await apiFetch("/estimates/{estimate_id}", "delete", {
      accessToken: token,
      params: { estimate_id: id },
    });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to delete estimate");
  }
}
