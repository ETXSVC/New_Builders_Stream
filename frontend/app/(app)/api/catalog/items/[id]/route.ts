import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/catalogs/items/{item_id}", "patch", {
      accessToken: token,
      params: { item_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update catalog item");
  }
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    await apiFetch("/catalogs/items/{item_id}", "delete", {
      accessToken: token,
      params: { item_id: id },
    });
    return new NextResponse(null, { status: 204 });
  } catch (err) {
    return errorResponse(err, "Failed to delete catalog item");
  }
}
