import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";
import { missingPlatformTokenResponse, platformToken } from "@/lib/platform/session";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ companyId: string }> }
) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId } = await params;
  try {
    const data = await apiFetch("/platform/companies/{company_id}", "get", {
      accessToken: token,
      params: { company_id: companyId },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load tenant");
  }
}

/** Rename. The only company field the console may change — see the schema. */
export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ companyId: string }> }
) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/platform/companies/{company_id}", "patch", {
      accessToken: token,
      params: { company_id: companyId },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to rename the tenant");
  }
}

/**
 * Takes a tenant out of service. SOFT — the backend sets
 * `companies.deleted_at` and holds no DELETE privilege on that table at all
 * (migration 0024), so nothing here can destroy a customer's data. `restore`
 * in ./restore/route.ts is the inverse.
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ companyId: string }> }
) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId } = await params;
  try {
    const data = await apiFetch("/platform/companies/{company_id}", "delete", {
      accessToken: token,
      params: { company_id: companyId },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to take the tenant out of service");
  }
}
