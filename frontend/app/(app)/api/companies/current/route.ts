import { NextRequest, NextResponse } from "next/server";
import { apiFetch, ApiError } from "@/lib/api/client";

export async function GET(request: NextRequest) {
  const authHeader = request.headers.get("Authorization");
  const accessToken = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : undefined;
  const companyId = request.nextUrl.searchParams.get("company_id");
  if (!accessToken || !companyId) {
    return NextResponse.json({ detail: "Missing access token or company_id" }, { status: 400 });
  }
  try {
    const data = await apiFetch("/companies/{company_id}", "get", {
      accessToken,
      params: { company_id: companyId },
    });
    return NextResponse.json(data);
  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.detail }, { status: err.status });
    }
    return NextResponse.json({ detail: "Failed to load company" }, { status: 502 });
  }
}
