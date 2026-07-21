"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCurrency } from "@/lib/format";

const ESTIMATE_STATUSES = ["draft", "sent", "approved", "rejected"] as const;

interface EstimateRow {
  id: string;
  status: string;
  total: string | null;
  parent_name: string | null;
}

export default function EstimatesPage() {
  const { accessToken, role } = useAuth();
  const [estimates, setEstimates] = React.useState<EstimateRow[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [statusFilter, setStatusFilter] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const requestGenRef = React.useRef(0);

  const canCreate = role === "admin" || role === "project_manager";

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
        const response = await fetch(`/api/estimates?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load estimates");
          return;
        }
        setEstimates((prev) => (replace ? data.items : [...prev, ...data.items]));
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
    void Promise.resolve().then(() => load(null, true));
  }, [load]);

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Estimates</h1>
        {canCreate && (
          <Link href="/estimates/new">
            <Button>New estimate</Button>
          </Link>
        )}
      </div>
      <Select
        aria-label="Filter by status"
        className="w-44"
        value={statusFilter}
        onChange={(e) => setStatusFilter(e.target.value)}
      >
        <option value="">All statuses</option>
        {ESTIMATE_STATUSES.map((s) => (
          <option key={s} value={s}>
            {s[0].toUpperCase() + s.slice(1)}
          </option>
        ))}
      </Select>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && estimates.length === 0 && !error && (
        <p className="text-sm text-slate-600">No estimates yet — create your first estimate.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {estimates.map((estimate) => (
          <li key={estimate.id}>
            <Link
              href={`/estimates/${estimate.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">{estimate.parent_name ?? "—"}</span>
              <span className="text-sm text-slate-600">{formatCurrency(estimate.total)}</span>
              <StatusBadge status={estimate.status} />
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
