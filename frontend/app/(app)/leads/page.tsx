"use client";

import * as React from "react";
import Link from "next/link";
import { useCursorList } from "@/lib/use-cursor-list";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { LEAD_STATUSES, labelFor } from "@/lib/state-machines";
import { formatCurrency, formatDate } from "@/lib/format";

interface Lead {
  id: string;
  contact_name: string;
  project_name: string;
  status: string;
  estimated_value: string | null;
  created_at: string;
}

export default function LeadsPage() {
  const [statusFilter, setStatusFilter] = React.useState("");
  // The generation guard, the replace-or-append and the cursor bookkeeping
  // now live in the hook (lib/use-cursor-list.ts). Changing `statusFilter`
  // re-runs the load as a *replace*, which is what makes an in-flight
  // Load-more from the previous filter discard itself rather than append
  // stale rows onto the new list.
  const {
    items: leads,
    nextCursor,
    loading,
    error,
    loadMore,
  } = useCursorList<Lead>({
    path: "/api/leads",
    params: { status: statusFilter },
    label: "leads",
  });

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Leads</h1>
        <Link href="/leads/new">
          <Button>New lead</Button>
        </Link>
      </div>
      <div className="flex items-center gap-2">
        <Select
          aria-label="Filter by status"
          className="w-44"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">All statuses</option>
          {LEAD_STATUSES.map((s) => (
            <option key={s} value={s}>
              {labelFor(s)}
            </option>
          ))}
        </Select>
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && leads.length === 0 && !error && (
        <p className="text-sm text-slate-600">No leads yet — create your first lead.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {leads.map((lead) => (
          <li key={lead.id}>
            <Link href={`/leads/${lead.id}`} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50">
              <span className="flex-1">
                <span className="block text-sm font-medium">{lead.contact_name}</span>
                <span className="block text-sm text-slate-600">{lead.project_name}</span>
              </span>
              <span className="text-sm text-slate-600">{formatCurrency(lead.estimated_value)}</span>
              <span className="text-sm text-slate-500">{formatDate(lead.created_at)}</span>
              <StatusBadge status={lead.status} />
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
