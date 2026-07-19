"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
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
  const { accessToken } = useAuth();
  const [leads, setLeads] = React.useState<Lead[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [statusFilter, setStatusFilter] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  // Request generation: a replace load (fresh filter) starts a new
  // generation, and any load resolving under an older one discards its
  // result — otherwise an in-flight Load-more from the previous filter
  // could append stale rows onto the new filter's list.
  const requestGenRef = React.useRef(0);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (statusFilter) params.set("status", statusFilter);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/leads?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load leads");
          return;
        }
        setLeads((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken, statusFilter]
  );

  React.useEffect(() => {
    load(null, true);
  }, [load]);

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
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </main>
  );
}
