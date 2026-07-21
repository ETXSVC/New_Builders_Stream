"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

export function Nav({ companyId }: { companyId: string }) {
  const router = useRouter();
  const { accessToken, role, clearSession } = useAuth();
  const [companyName, setCompanyName] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!accessToken) return;
    fetch(`/api/companies/current?company_id=${companyId}`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok) {
          // The route handler always returns valid JSON, even on failure
          // (e.g. a stale access token, a bad company_id) — so a plain
          // .then(r => r.json()) never distinguishes this from success.
          // Surface it rather than silently falling back to the generic
          // name with no signal anything went wrong.
          console.error("Failed to load company name:", data.detail ?? r.statusText);
          setCompanyName(null);
          return;
        }
        setCompanyName(data.name ?? null);
      })
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
        {(role === "admin" || role === "project_manager") && (
          <Link href="/leads" className="text-sm text-slate-600 hover:text-slate-900">
            Leads
          </Link>
        )}
        {(role === "admin" || role === "project_manager" || role === "accountant") && (
          <Link href="/projects" className="text-sm text-slate-600 hover:text-slate-900">
            Projects
          </Link>
        )}
        {(role === "admin" || role === "project_manager" || role === "accountant") && (
          <Link href="/estimates" className="text-sm text-slate-600 hover:text-slate-900">
            Estimates
          </Link>
        )}
        {(role === "admin" || role === "project_manager") && (
          <Link href="/catalog" className="text-sm text-slate-600 hover:text-slate-900">
            Catalog
          </Link>
        )}
        {role === "field_crew" && (
          <Link href="/my-tasks" className="text-sm text-slate-600 hover:text-slate-900">
            My tasks
          </Link>
        )}
        <Link href="/account" className="text-sm text-slate-600 hover:text-slate-900">
          Account
        </Link>
        <Button variant="outline" size="sm" onClick={handleLogout}>
          Log out
        </Button>
      </div>
    </header>
  );
}
