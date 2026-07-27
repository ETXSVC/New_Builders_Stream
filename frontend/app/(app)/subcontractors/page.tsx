"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { useCursorList } from "@/lib/use-cursor-list";
import { Button } from "@/components/ui/button";

interface SubcontractorRow {
  id: string;
  name: string;
  trade: string | null;
  contact_email: string | null;
}

export default function SubcontractorsPage() {
  const { role } = useAuth();
  const {
    items: subcontractors,
    nextCursor,
    loading,
    error,
    loadMore,
  } = useCursorList<SubcontractorRow>({
    path: "/api/subcontractors",
    label: "subcontractors",
  });

  // Creation is admin-only on the backend (POST /subcontractors).
  const canCreate = role === "admin";

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Subcontractors</h1>
        {canCreate && (
          <Link href="/subcontractors/new">
            <Button>New subcontractor</Button>
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && subcontractors.length === 0 && !error && (
        <p className="text-sm text-slate-600">No subcontractors yet.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {subcontractors.map((sub) => (
          <li key={sub.id}>
            <Link
              href={`/subcontractors/${sub.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">{sub.name}</span>
              <span className="text-sm text-slate-600">{sub.trade ?? "—"}</span>
              <span className="text-sm text-slate-500">{sub.contact_email ?? ""}</span>
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={loadMore} disabled={loading}>
          Load more
        </Button>
      )}
    </main>
  );
}
