import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { errorResponse } from "@/lib/api/handler-utils";
import { missingPlatformTokenResponse, platformToken } from "@/lib/platform/session";

// Renamed on destructuring purely to keep a binding called `module` out of
// local scope; the segment is `[module]`, matching the backend's own path.
type Params = { params: Promise<{ companyId: string; module: string }> };

/** Grant (`enabled: true`) or withhold (`enabled: false`) a module. */
export async function PUT(request: NextRequest, { params }: Params) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId, module: moduleName } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/platform/companies/{company_id}/modules/{module}", "put", {
      accessToken: token,
      params: { company_id: companyId, module: moduleName },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to set the module override");
  }
}

/**
 * Removes the override so the module defers to the tier again. This is the
 * third state, not a synonym for `enabled: false` — see the override table in
 * the UI and migration 0023.
 */
export async function DELETE(request: NextRequest, { params }: Params) {
  const token = platformToken(request);
  if (!token) return missingPlatformTokenResponse();
  const { companyId, module: moduleName } = await params;
  try {
    const data = await apiFetch("/platform/companies/{company_id}/modules/{module}", "delete", {
      accessToken: token,
      params: { company_id: companyId, module: moduleName },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to clear the module override");
  }
}
