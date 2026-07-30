import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";
import { missingPlatformTokenResponse, platformToken } from "@/lib/platform/session";

/** Puts a tenant taken out of service back. See the DELETE in ../route.ts. */
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ companyId: string }> }
) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId } = await params;
  try {
    const data = await apiFetch("/platform/companies/{company_id}/restore", "post", {
      accessToken: token,
      params: { company_id: companyId },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to restore the tenant");
  }
}
