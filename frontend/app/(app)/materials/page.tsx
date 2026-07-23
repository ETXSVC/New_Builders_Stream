"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";

interface BomLine {
  id: string;
  project_id: string;
  description: string;
  unit: string;
  quantity: string;
  quantity_received: string;
  status: string;
}

const STATUS_FILTERS = ["All", "needed", "ordered", "partially_received", "received"] as const;

export default function MaterialsPage() {
  const { accessToken } = useAuth();
  const [lines, setLines] = React.useState<BomLine[]>([]);
  const [statusFilter, setStatusFilter] = React.useState<(typeof STATUS_FILTERS)[number]>("All");
  const [error, setError] = React.useState<string | null>(null);

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: BomLine[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/materials?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load materials");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setLines(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  const visibleLines = statusFilter === "All" ? lines : lines.filter((line) => line.status === statusFilter);

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">Materials</h1>
      <Select
        className="w-56"
        value={statusFilter}
        onChange={(e) => setStatusFilter(e.target.value as (typeof STATUS_FILTERS)[number])}
      >
        {STATUS_FILTERS.map((filter) => (
          <option key={filter} value={filter}>
            {filter === "All" ? "All statuses" : filter}
          </option>
        ))}
      </Select>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {visibleLines.map((line) => (
          <li key={line.id} className="flex items-center gap-3 px-4 py-3 text-sm">
            <span className="flex-1">{line.description}</span>
            <span className="text-slate-500">
              {line.quantity_received} / {line.quantity} {line.unit}
            </span>
            <StatusBadge status={line.status} />
          </li>
        ))}
        {visibleLines.length === 0 && (
          <li className="px-4 py-3 text-sm text-slate-500">No materials match this filter.</li>
        )}
      </ul>
    </main>
  );
}
