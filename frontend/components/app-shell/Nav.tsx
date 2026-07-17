"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

export function Nav({ companyId }: { companyId: string }) {
  const router = useRouter();
  const { accessToken, clearSession } = useAuth();
  const [companyName, setCompanyName] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!accessToken) return;
    fetch(`/api/companies/current?company_id=${companyId}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
      .then((r) => r.json())
      .then((data) => setCompanyName(data.name ?? null))
      .catch(() => setCompanyName(null));
  }, [accessToken, companyId]);

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    clearSession();
    router.push("/login");
  }

  return (
    <header className="border-b border-slate-200 px-6 py-4 flex items-center justify-between">
      <span className="font-semibold">{companyName ?? "Builders Stream"}</span>
      <div className="flex items-center gap-4">
        <a href="/account" className="text-sm text-slate-600 hover:text-slate-900">
          Account
        </a>
        <Button variant="outline" size="sm" onClick={handleLogout}>
          Log out
        </Button>
      </div>
    </header>
  );
}
