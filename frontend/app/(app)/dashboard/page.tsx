"use client";

import { useAuth } from "@/contexts/AuthContext";
import { Nav } from "@/components/app-shell/Nav";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

export default function DashboardPage() {
  const { accessToken } = useAuth();

  // Foundation ships no real dashboard content — every later sub-project
  // (CRM+PM, Estimation, Compliance+Billing, Invoicing, Integrations)
  // adds its own screens here. This page exists solely to prove the
  // authenticated shell renders and the session survives navigation.
  return (
    <div>
      {/* company_id will come from a real session/company-context once a
          later sub-project needs to switch companies; for Foundation the
          JWT's default_company_id (decoded client-side from the access
          token's payload — not verified client-side, display only) is
          the only company a fresh registration ever has. */}
      <Nav companyId={decodeCompanyId(accessToken)} />
      <main className="p-6">
        <Card className="max-w-md">
          <CardHeader>
            <CardTitle>Welcome</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-slate-600">
              This is a placeholder. Real project, lead, and estimate screens land in later sub-projects.
            </p>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}

function decodeCompanyId(accessToken: string | null): string {
  if (!accessToken) return "";
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return payload.default_company_id ?? "";
  } catch {
    return "";
  }
}
