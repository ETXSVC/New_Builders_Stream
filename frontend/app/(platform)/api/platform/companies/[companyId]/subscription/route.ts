import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";
import { missingPlatformTokenResponse, platformToken } from "@/lib/platform/session";

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ companyId: string }> }
) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/platform/companies/{company_id}/subscription", "patch", {
      accessToken: token,
      params: { company_id: companyId },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update the subscription");
  }
}
