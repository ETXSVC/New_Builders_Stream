"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { ProjectScopeSelect } from "@/components/billing/ProjectScopeSelect";
import { formatCurrency } from "@/lib/format";

interface InvoiceRow {
  id: string;
  invoice_number: string;
  amount: string;
  status: string;
  due_date: string | null;
  outstanding_balance: string;
}

export function InvoiceList() {
  const { accessToken } = useAuth();
  const [projectId, setProjectId] = React.useState("");
  const [invoices, setInvoices] = React.useState<InvoiceRow[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const requestGenRef = React.useRef(0);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken || !projectId) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/invoices?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load invoices");
          return;
        }
        setInvoices((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken, projectId]
  );

  React.useEffect(() => {
    // Deferred so no setState runs synchronously inside the effect body
    // (react-hooks/set-state-in-effect), same pattern as the estimates page.
    void Promise.resolve().then(() => {
      setInvoices([]);
      setNextCursor(null);
      if (projectId) void load(null, true);
    });
  }, [projectId, load]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <ProjectScopeSelect value={projectId} onChange={setProjectId} />
        {projectId && (
          <Link href={`/billing/invoices/new?project_id=${projectId}`}>
            <Button>New invoice</Button>
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!projectId && (
        <p className="text-sm text-slate-600">Select a project to see its invoices.</p>
      )}
      {projectId && !loading && invoices.length === 0 && !error && (
        <p className="text-sm text-slate-600">No invoices for this project yet.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {invoices.map((invoice) => (
          <li key={invoice.id}>
            <Link
              href={`/billing/invoices/${invoice.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">{invoice.invoice_number}</span>
              <span className="text-sm text-slate-600">{formatCurrency(invoice.amount)}</span>
              <span className="text-sm text-slate-500">
                {formatCurrency(invoice.outstanding_balance)} due
              </span>
              <StatusBadge status={invoice.status} />
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </div>
  );
}
