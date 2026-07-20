"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { SummaryCards } from "@/components/dashboard/SummaryCards";

export default function DashboardPage() {
  const router = useRouter();
  const { accessToken, role, isHydrating } = useAuth();

  // Role landing rules (spec Decision 3): field_crew's home is My Tasks;
  // a client's home is their project (or the sanitized projects list when
  // they have several). admin/PM/accountant stay here.
  React.useEffect(() => {
    if (isHydrating || !accessToken) return;
    if (role === "field_crew") {
      router.replace("/my-tasks");
      return;
    }
    if (role === "client") {
      (async () => {
        try {
          const response = await fetch("/api/projects", {
            headers: { Authorization: `Bearer ${accessToken}` },
          });
          const data = await response.json();
          if (response.ok && data.items.length === 1) {
            router.replace(`/projects/${data.items[0].id}`);
          } else {
            router.replace("/projects");
          }
        } catch {
          router.replace("/projects");
        }
      })();
    }
  }, [isHydrating, accessToken, role, router]);

  const isStaffDashboard = role === "admin" || role === "project_manager" || role === "accountant";

  return (
    <main className="p-6 flex flex-col gap-6">
      <h1 className="text-xl font-semibold">Dashboard</h1>
      {!isStaffDashboard && <p className="text-sm text-slate-500">Loading your workspace…</p>}
      {isStaffDashboard && (
        <>
          {(role === "admin" || role === "project_manager") && <SummaryCards />}
          <div className="flex gap-4 text-sm">
            {(role === "admin" || role === "project_manager") && (
              <Link href="/leads" className="underline text-slate-700">
                Go to leads
              </Link>
            )}
            <Link href="/projects" className="underline text-slate-700">
              Go to projects
            </Link>
          </div>
        </>
      )}
    </main>
  );
}
